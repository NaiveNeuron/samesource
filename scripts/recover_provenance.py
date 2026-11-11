#!/usr/bin/env python3
"""Locate each stored PkgBridge recipe file in its distribution's own history.

The PkgBridge acquisition code records no upstream packaging revision. This
script searches public packaging histories for byte-identical copies (same git
blob id, or same bytes inside a snapshot.debian.org source package) and records
the earliest commit or source version that contains them. A match establishes
where those bytes were published; it does not establish build success,
software identity, or redistribution rights.
"""

from __future__ import annotations

from datetime import datetime
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
CACHE = Path.home() / ".cache" / "pkgbridge" / "provenance"
AGENT = {"User-Agent": "pkgbridge-provenance (https://github.com/Aethelsirius/pkgbridge)"}


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=120) as r:
        return r.read()


def bare_clone(url: str, dest: Path, since: str | None = None) -> bool:
    if dest.exists():
        return True
    args = ["clone", "--quiet", "--bare", "--filter=blob:none"]
    if since:
        args += [f"--shallow-since={since}"]
    for attempt in range(3):
        try:
            git(*args, url, str(dest))
            return True
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            if "not found" in exc.stderr.lower() or "does not exist" in exc.stderr.lower():
                return False
            time.sleep(5 * (attempt + 1))
    return False


def blob_history(repo: Path, wanted: set[str], paths: list[str] | None = None) -> dict:
    """Earliest commit (by commit date) that adds or modifies each wanted blob."""
    args = ["log", "--all", "--reverse", "--raw", "--no-abbrev", "--no-renames",
            "--format=C %H %cI"]
    if paths:
        args += ["--", *paths]
    found: dict[str, dict] = {}
    proc = subprocess.Popen(["git", *args], cwd=repo, stdout=subprocess.PIPE, text=True)
    commit = date = None
    assert proc.stdout
    for line in proc.stdout:
        if line.startswith("C "):
            _, commit, date = line.split()
        elif line.startswith(":"):
            meta, path = line.rstrip("\n").split("\t", 1)
            blob = meta.split()[3]
            if blob in wanted and blob not in found:
                found[blob] = {"commit": commit, "date": date, "path": path}
    proc.wait()
    return found


def per_package(family: str, urls: list[str], name: str, blob: str) -> dict:
    """Search every candidate repository; report the earliest exact match."""
    tried, hits = [], []
    for url in urls:
        host = url.split("/")[2]
        repo = CACHE / family / host / f"{name}.git"
        if not bare_clone(url, repo):
            tried.append({"repository": url, "status": "repository_unavailable"})
            continue
        hit = blob_history(repo, {blob}).get(blob)
        tried.append({"repository": url, "status": "identical_bytes" if hit else "not_found",
                      **(hit or {})})
        if hit:
            hits.append({"repository": url, **hit})
    if not hits:
        return {"status": "not_found", "searched": tried}
    first = min(hits, key=lambda h: datetime.fromisoformat(h["date"]))
    return {"status": "identical_bytes", **first, "searched": tried}


def debian(name: str, dsc_path: Path, rules: Path, control: Path) -> dict:
    text = dsc_path.read_text()
    fields = dict(line.split(": ", 1) for line in text.splitlines()
                  if ": " in line and not line.startswith(" "))
    source, version = fields["Source"], fields["Version"]
    base = f"https://snapshot.debian.org/mr/package/{source}/{version}/srcfiles?fileinfo=1"
    try:
        info = json.loads(fetch(base))
    except Exception as exc:  # noqa: BLE001 - record and continue
        return {"status": "snapshot_unavailable", "source": source, "version": version,
                "error": type(exc).__name__}
    names = {entry["name"]: sha for sha, entries in info["fileinfo"].items() for entry in entries}
    dsc_name = f"{source}_{version.split(':')[-1]}.dsc"
    dsc_sha1 = hashlib.sha1(dsc_path.read_bytes()).hexdigest()
    record = {"source": source, "version": version, "dsc_name": dsc_path.name,
              "dsc_sha1": dsc_sha1, "dsc_identical": names.get(dsc_name) == dsc_sha1,
              "control_sha256": hashlib.sha256(control.read_bytes()).hexdigest(),
              "rules_sha256": hashlib.sha256(rules.read_bytes()).hexdigest()}
    debian_tar = next((n for n in names if n.endswith((".debian.tar.xz", ".debian.tar.gz",
                                                      ".diff.gz"))), None)
    if debian_tar and not debian_tar.endswith(".diff.gz"):
        record["debian_tar"] = {"name": debian_tar, "sha1": names[debian_tar]}
        data = fetch(f"https://snapshot.debian.org/file/{names[debian_tar]}")
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = {m.name.removeprefix("./"): m for m in tar.getmembers()}
            for label, local in (("control", control), ("rules", rules)):
                member = members.get(f"debian/{label}")
                remote = tar.extractfile(member).read() if member else None
                record[f"{label}_identical"] = remote == local.read_bytes()
    else:
        record["control_identical"] = record["rules_identical"] = None
    ok = record["dsc_identical"] and record["control_identical"] and record["rules_identical"]
    record["status"] = "identical_bytes" if ok else "partial_or_different"
    record["snapshot"] = f"https://snapshot.debian.org/package/{source}/{version}/"
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--families", default="deb,rpm,pkgbuild,ebuild,nix")
    parser.add_argument("--since", default="2024-01-01",
                        help="history depth for the large gentoo and nixpkgs clones")
    parser.add_argument("--output", type=Path, default=PROJECT / "evidence" / "provenance.json")
    args = parser.parse_args()
    families = args.families.split(",")

    src = args.source_root
    commit = git("rev-parse", "HEAD", cwd=src).strip()
    catalog = json.loads((src / "package_list.json").read_text())["packages"]
    blobs = {}
    for line in git("ls-files", "-s", "dataset", cwd=src).splitlines():
        meta, path = line.split("\t")
        blobs[path] = meta.split()[1]

    previous = json.loads(args.output.read_text())["records"] if args.output.exists() else []
    records = {(r["family"], r["name"]): r for r in previous if r["family"] not in families}
    jobs = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for entry in catalog:
            name = entry["name"]
            if "rpm" in families:
                path = f"dataset/rpm/{name}/{name}.spec"
                path = path if path in blobs else next(p for p in blobs if p.startswith(f"dataset/rpm/{name}/"))
                jobs[("rpm", name, path)] = pool.submit(
                    per_package, "rpm", [f"https://src.fedoraproject.org/rpms/{entry['rpm']}.git"],
                    entry["rpm"], blobs[path])
            if "pkgbuild" in families:
                path = f"dataset/pkgbuild/{name}/PKGBUILD"
                jobs[("pkgbuild", name, path)] = pool.submit(
                    per_package, "pkgbuild",
                    [f"https://gitlab.archlinux.org/archlinux/packaging/packages/{entry['pkgbuild']}.git",
                     f"https://aur.archlinux.org/{entry['pkgbuild']}.git"],
                    entry["pkgbuild"], blobs[path])
            if "deb" in families:
                base = src / "dataset" / "deb" / name
                dsc = next(base.glob("*.dsc"))
                jobs[("deb", name, str(dsc.relative_to(src)))] = pool.submit(
                    debian, name, dsc, base / "rules", base / "control")
        for key, future in jobs.items():
            family, name, path = key
            records[(family, name)] = {"family": family, "name": name, "file": path,
                                       "blob": blobs.get(path), **future.result()}

    for family, url, sub in (("ebuild", "https://github.com/gentoo/gentoo.git", "ebuild"),
                             ("nix", "https://github.com/NixOS/nixpkgs.git", "nixpkgs")):
        if family not in families:
            continue
        repo = CACHE / f"{family}-monorepo.git"
        if not bare_clone(url, repo, since=args.since):
            raise SystemExit(f"clone failed: {url}")
        files = {p: b for p, b in blobs.items() if p.startswith(f"dataset/{sub}/")}
        paths = ([entry["ebuild"] for entry in catalog] if family == "ebuild" else None)
        hits = blob_history(repo, set(files.values()), paths)
        for path, blob in files.items():
            name = path.split("/")[2]
            hit = hits.get(blob)
            records[(family, name)] = {
                "family": family, "name": name, "file": path, "blob": blob, "repository": url,
                "history_since": args.since,
                **({"status": "identical_bytes", **hit} if hit else {"status": "not_found"})}

    ordered = sorted(records.values(), key=lambda r: (r["family"], r["name"]))
    summary: dict[str, dict[str, int]] = {}
    for r in ordered:
        added = git("log", "--diff-filter=A", "--format=%cI", "--", r["file"], cwd=src).split()
        r["pkgbridge_added"] = added[-1] if added else None
        if r.get("date") and added:
            r["occurrence_predates_collection"] = (
                datetime.fromisoformat(r["date"]) <= datetime.fromisoformat(added[-1]))
        summary.setdefault(r["family"], {}).setdefault(r["status"], 0)
        summary[r["family"]][r["status"]] += 1
    args.output.write_text(json.dumps({
        "schema": "pkgbridge-provenance/1", "source_commit": commit,
        "interpretation": "identical_bytes means the stored file's bytes occur in the named "
                          "public history; it does not establish identity, buildability or rights.",
        "summary": summary, "records": ordered}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
