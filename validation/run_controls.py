#!/usr/bin/env python3
"""Run the prespecified evaluator controls in validation/controls.json.

Each control builds a stored target-family reference recipe from the pinned
PkgBridge commit, plus deliberately broken variants, in digest-pinned images.
Observed stage outcomes are compared with the expectations frozen in the
control file. No model is invoked; nothing here measures translation quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
TARGETS = PROJECT / "validation" / "targets"
STAGES = ["S", "B", "M", "I", "K", "F"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pkgbridge-validation"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


STUB = "printf '\\043!/bin/sh\\nexit 0\\n' > {path} && chmod 755 {path}"


def mutate(target: str, variant: str, files: dict[str, str]) -> dict[str, str]:
    """Apply one prespecified, deterministic defect to the reference recipe."""
    files = dict(files)
    if variant == "reference":
        return files
    flip = lambda m: m.group(1) + ("0" if m.group(2) != "0" else "1")  # noqa: E731
    if target == "deb":
        dsc = next(name for name in files if name.endswith(".dsc"))
        if variant == "bad_source":
            files[dsc] = re.sub(r"(Checksums-Sha256:\n )([0-9a-f])", flip, files[dsc], count=1)
        elif variant == "no_install":
            files["rules"] = re.sub(r"override_dh_install:\n(\t.*\n?)+",
                                    "override_dh_install:\n\t:\n", files["rules"])
        else:
            bindir = "$(CURDIR)/debian/pigz/usr/bin"
            rules = files["rules"]
            rules = rules.replace(f"\tcp -l pigz {bindir}/unpigz\n",
                                  "\t" + STUB.format(path=f"{bindir}/unpigz") + "\n")
            rules = rules.replace(f"\tcp -l pigz {bindir}\n",
                                  "\t" + STUB.format(path=f"{bindir}/pigz") + "\n")
            files["rules"] = rules
    elif target == "rpm":
        spec = next(iter(files))
        if variant == "bad_source":
            files[spec] = re.sub(r"^(Version:\s+).*$", r"\g<1>1.0.99", files[spec],
                                 count=1, flags=re.M)
        elif variant == "no_install":
            files[spec] = re.sub(r"^%make_install[^\n]*\n(?:[ \t]+[^\n]*\n)*", "", files[spec],
                                 flags=re.M)
        else:
            stub = STUB.format(path="%{buildroot}%{_bindir}/colordiff")
            files[spec] = files[spec].replace("\n\n\n%files", f"\n{stub}\n\n\n%files", 1)
    elif target == "pkgbuild":
        if variant == "bad_source":
            files["PKGBUILD"] = re.sub(r"(b2sums=\(')([0-9a-f])", flip, files["PKGBUILD"], count=1)
        elif variant == "no_install":
            files["PKGBUILD"] = re.sub(r"package\(\) \{\n(.*\n)*?\}", "package() {\n  :\n}",
                                       files["PKGBUILD"])
        else:
            stub = STUB.format(path='"$pkgdir/usr/bin/cloc"')
            files["PKGBUILD"] = re.sub(r"(package\(\) \{\n(.*\n)*?)\}",
                                       lambda m: m.group(1) + f"  {stub}\n}}", files["PKGBUILD"])
    return files


def docker(image: str, work: Path, script: str, env: dict[str, str], platform: str) -> int:
    args = ["docker", "run", "--rm", "--platform", platform, "-v", f"{work}:/work"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += [image, "bash", f"/work/{script}"]
    with (work / "logs" / f"docker-{script}.log").open("w") as log:
        return subprocess.run(args, stdout=log, stderr=subprocess.STDOUT).returncode


def resolved_digest(image: str, platform: str) -> str:
    subprocess.run(["docker", "pull", "-q", "--platform", platform, image],
                   check=True, stdout=subprocess.DEVNULL)
    out = subprocess.check_output(["docker", "image", "inspect", image,
                                   "--format", "{{json .RepoDigests}}"], text=True)
    return ",".join(json.loads(out))


def run_one(control: dict, variant: str, spec: dict, source: Path, root: Path) -> dict:
    target, package = control["target"], control["package"]
    work = root / f"{target}-{package}-{variant}"
    shutil.rmtree(work, ignore_errors=True)
    (work / "logs").mkdir(parents=True)
    (work / "recipe").mkdir()

    original = {Path(p).name: (source / p).read_text() for p in control["recipe_files"]}
    recipe = mutate(target, variant, original)
    mutated = sorted(name for name in recipe if recipe[name] != original[name])
    if variant != "reference" and not mutated:
        raise RuntimeError(f"{target}/{variant}: mutation did not change the recipe")
    for name, text in recipe.items():
        (work / "recipe" / name).write_text(text)

    env = {"SNAPSHOT": control["repository_snapshot"], "TARGET": target, "PKG": package,
           "SMOKE": control["smoke"], "BEHAVIOUR": control["behaviour"]}
    if target == "deb":
        (work / "sources").mkdir()
        dsc = original[next(n for n in original if n.endswith(".dsc"))]
        block = dsc.split("Checksums-Sha1:\n", 1)[1].split("\n", 3)
        for line in block[:2]:
            sha1, _size, name = line.split()
            data = fetch(f"https://snapshot.debian.org/file/{sha1}")
            if hashlib.sha1(data).hexdigest() != sha1:
                raise RuntimeError(f"snapshot returned wrong bytes for {name}")
            (work / "sources" / name).write_bytes(data)
    if target == "rpm":
        (work / "sources_reference").write_bytes(fetch(control["source_checksum_reference"]))
    shutil.copy(TARGETS / f"{target}.sh", work / "build.sh")
    shutil.copy(TARGETS / "install.sh", work / "install.sh")

    started = time.time()
    platform = control.get("platform", spec["platform"])
    docker(control["image"], work, "build.sh", env, platform)
    observed = parse_stages(work / "stages.env")
    if observed.get("B") == "pass":
        manifest = (work / "logs" / "manifest.txt").read_text().split()
        missing = [p for p in control["expected_paths"] if p not in manifest]
        observed["M"] = "fail" if missing else "pass"
        docker(control["image"], work, "install.sh", env, platform)
        observed.update(parse_stages(work / "stages.env"))
    else:
        missing = None
    outcome = {stage: observed.get(stage, "not_evaluated") for stage in STAGES}
    if "SETUP" in observed:
        outcome = {stage: "infrastructure" for stage in STAGES}

    variants = spec["variants"][variant]
    expected = variants.get("expected") or variants["expected_by_target"][target]
    packages = sorted((work / "out").glob("*")) if (work / "out").exists() else []
    toolchain = work / "logs" / "toolchain.txt"
    return {
        "target": target, "package": package, "variant": variant,
        "image_requested": control["image"],
        "image_resolved": resolved_digest(control["image"], platform),
        "platform": platform,
        "repository_snapshot": control["repository_snapshot"],
        "recipe_sha256": {n: sha256(t.encode()) for n, t in sorted(recipe.items())},
        "mutated_files": mutated,
        "reference_files_match_snapshot": {k[6:]: v for k, v in observed.items()
                                           if k.startswith("MATCH_")},
        "observed": outcome, "expected": expected,
        "agrees_with_expectation": outcome == expected,
        "missing_expected_paths": missing,
        "built_packages": {p.name: sha256(p.read_bytes()) for p in packages},
        "toolchain_sha256": sha256(toolchain.read_bytes()) if toolchain.exists() else None,
        "seconds": round(time.time() - started),
        "work_dir": str(work),
    }


def parse_stages(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().split())


def verify_inputs(spec: dict, root: Path, manifest: Path) -> None:
    """Refuse to run unless every control recipe matches the provenance manifest."""
    records = {(r["family"], r["name"]): r for r in json.loads(manifest.read_text())["records"]}
    for control in spec["controls"]:
        record = records[(control["target"], control["package"])]
        for rel in control["recipe_files"]:
            data = (root / rel).read_bytes()
            name = Path(rel).name
            if control["target"] == "deb":
                ok = (hashlib.sha1(data).hexdigest() == record["dsc_sha1"] if name.endswith(".dsc")
                      else sha256(data) == record[f"{name}_sha256"])
            else:
                ok = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == record["blob"]
            if not ok:
                raise SystemExit(f"{rel} does not match {manifest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True,
                        help="corpus root (fetch_corpus.py output or PkgBridge checkout)")
    parser.add_argument("--manifest", type=Path, default=PROJECT / "evidence" / "provenance.json")
    parser.add_argument("--work-root", type=Path,
                        default=Path.home() / ".cache" / "pkgbridge" / "validation")
    parser.add_argument("--output", type=Path, default=PROJECT / "evidence" / "release-validation.json")
    parser.add_argument("--only", help="run only TARGET or TARGET:VARIANT")
    args = parser.parse_args()

    spec = json.loads((PROJECT / "validation" / "controls.json").read_text())
    verify_inputs(spec, args.source_root, args.manifest)
    commit = spec["source_commit"]

    previous = json.loads(args.output.read_text())["runs"] if args.output.exists() else []
    runs = {(r["target"], r["variant"]): r for r in previous}
    for control in spec["controls"]:
        for variant in spec["variants"]:
            key = f"{control['target']}:{variant}"
            if args.only and args.only not in (control["target"], key):
                continue
            print(f"== {key}", flush=True)
            result = run_one(control, variant, spec, args.source_root, args.work_root)
            print(json.dumps(result["observed"]), "agrees" if result["agrees_with_expectation"]
                  else "DISAGREES", f"{result['seconds']}s", flush=True)
            prior = runs.get((control["target"], variant))
            if prior is not None:
                history = prior.pop("previous_attempts", [])
                result["previous_attempts"] = history + [
                    {k: prior[k] for k in ("observed", "agrees_with_expectation", "seconds")}]
            runs[(control["target"], variant)] = result
    order = {c["target"]: i for i, c in enumerate(spec["controls"])}
    vorder = {v: i for i, v in enumerate(spec["variants"])}
    ordered = sorted(runs.values(), key=lambda r: (order[r["target"]], vorder[r["variant"]]))
    args.output.write_text(json.dumps({
        "schema": "pkgbridge-release-validation/1",
        "controls_sha256": sha256((PROJECT / "validation" / "controls.json").read_bytes()),
        "corpus_selection_commit": commit,
        "provenance_manifest_sha256": sha256(args.manifest.read_bytes()),
        "runs": ordered,
    }, indent=2) + "\n")
    # A recorded disagreement must not look like a successful replay.
    if not all(r["agrees_with_expectation"] for r in ordered):
        raise SystemExit(1)


if __name__ == "__main__":
    os.umask(0o022)
    main()
