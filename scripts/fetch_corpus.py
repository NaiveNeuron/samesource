#!/usr/bin/env python3
"""Rebuild the recipe corpus from public distribution sources, verifying bytes.

Reads evidence/provenance.json, downloads each recorded file from the named
upstream revision (packaging git hosts or snapshot.debian.org), and checks it
against the recorded git blob id or SHA-1/SHA-256 before writing it under
OUT/dataset/<family>/<name>/. No PkgBridge code or copied recipe is needed.
Records without a byte-identical upstream match are reported, not fetched.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.parse
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
AGENT = {"User-Agent": "pkgbridge-fetch (https://github.com/Aethelsirius/pkgbridge)"}
DIRS = {"deb": "deb", "rpm": "rpm", "pkgbuild": "pkgbuild", "ebuild": "ebuild", "nix": "nixpkgs"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=120) as r:
        return r.read()


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def raw_url(record: dict) -> str:
    repo, commit, path = record["repository"], record["commit"], record["path"]
    quoted = urllib.parse.quote(path)
    if "src.fedoraproject.org" in repo:
        name = repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[1]
        return f"https://src.fedoraproject.org/rpms/{name}/raw/{commit}/f/{quoted}"
    if "gitlab.archlinux.org" in repo:
        return f"{repo.removesuffix('.git')}/-/raw/{commit}/{quoted}"
    if "aur.archlinux.org" in repo:
        name = repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[1]
        return f"https://aur.archlinux.org/cgit/aur.git/plain/{quoted}?h={name}&id={commit}"
    if "github.com" in repo:
        slug = repo.removeprefix("https://github.com/").removesuffix(".git")
        return f"https://raw.githubusercontent.com/{slug}/{commit}/{quoted}"
    raise ValueError(f"no raw-file rule for {repo}")


def rebuild(record: dict, out: Path) -> tuple[str, str]:
    family, name = record["family"], record["name"]
    target = out / "dataset" / DIRS[family] / name
    if record["status"] != "identical_bytes":
        return f"{family}/{name}", f"skipped ({record['status']})"
    if family == "deb":
        dsc = fetch(f"https://snapshot.debian.org/file/{record['dsc_sha1']}")
        if hashlib.sha1(dsc).hexdigest() != record["dsc_sha1"]:
            return f"{family}/{name}", "hash mismatch: dsc"
        tar_bytes = fetch(f"https://snapshot.debian.org/file/{record['debian_tar']['sha1']}")
        files = {record["dsc_name"]: dsc}
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            members = {m.name.removeprefix("./"): m for m in tar.getmembers()}
            for label in ("control", "rules"):
                data = tar.extractfile(members[f"debian/{label}"]).read()
                if hashlib.sha256(data).hexdigest() != record[f"{label}_sha256"]:
                    return f"{family}/{name}", f"hash mismatch: {label}"
                files[label] = data
    else:
        data = fetch(raw_url(record))
        if git_blob(data) != record["blob"]:
            return f"{family}/{name}", "hash mismatch"
        files = {Path(record["file"]).name: data}
    target.mkdir(parents=True, exist_ok=True)
    for filename, data in files.items():
        (target / filename).write_bytes(data)
    return f"{family}/{name}", "verified"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=PROJECT / "evidence" / "provenance.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--families", default=",".join(DIRS))
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--compare-with", type=Path,
                        help="another corpus root (e.g. a PkgBridge checkout) to compare trees")
    parser.add_argument("--record", type=Path, help="write a JSON record of this run")
    args = parser.parse_args()
    wanted = set(args.families.split(","))
    records = [r for r in json.loads(args.manifest.read_text())["records"]
               if r["family"] in wanted]
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda r: rebuild(r, args.out), records))
    seconds = round(time.time() - started, 1)
    counts: dict[str, int] = {}
    for key, status in results:
        counts[status] = counts.get(status, 0) + 1
        if status != "verified":
            print(f"{key}: {status}")
    print(json.dumps(counts, indent=2))
    if args.record:
        rebuilt = tree_digest(args.out / "dataset")
        record = {"schema": "pkgbridge-reconstruction/1", "date": time.strftime("%Y-%m-%d"),
                  "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                  "counts": counts, "seconds": seconds, "rebuilt_tree": rebuilt}
        if args.compare_with:
            other = tree_digest(args.compare_with / "dataset")
            record["compared_tree"] = other
            record["trees_identical"] = other == rebuilt
        args.record.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({k: record[k] for k in record if k != "counts"}, indent=2))
    if any(status.startswith("hash mismatch") for _, status in results):
        raise SystemExit(1)


def tree_digest(root: Path) -> dict:
    """SHA-256 over sorted (relative path, file SHA-256) pairs."""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    lines = [f"{p.relative_to(root)}\t{hashlib.sha256(p.read_bytes()).hexdigest()}" for p in files]
    return {"files": len(files), "sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest()}


if __name__ == "__main__":
    main()
