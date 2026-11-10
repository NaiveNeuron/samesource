#!/usr/bin/env python3
"""Retrieve the pinned packaging state of SameSource records into a directory.

Usage: fetch_recipe.py RECORD_ID [RECORD_ID ...] --out DIR [--db samesource.sqlite]

  deb       the .dsc, the packaging tarball, and for native packages the source tarball
  rpm       the dist-git tree at the Koji-pinned commit (shallow git fetch)
  pkgbuild  the Arch packaging repository archive at the pinned commit
  ebuild    the ebuild and the package's files/ directory at the pinned
            gentoo-mirror commit (via the GitHub API); eclasses are not copied,
            they are at the same commit of the same repository
Upstream source archives are not downloaded; their URLs and digests are in
the artifacts and digests tables.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import urllib.error
import urllib.request
import hashlib
import time

AGENT = {"User-Agent": "samesource-fetch (research dataset)"}
DEFAULT_DB = Path(__file__).resolve().parents[2] / "output" / "dataset" / "samesource.sqlite"


def get(url: str) -> bytes:
    for attempt in range(4):  # snapshot.debian.org answers 5xx intermittently
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def arch_project(pkgbase: str) -> str:
    # GitLab path rule used by Arch devtools.
    name = pkgbase.replace("+", "plus")
    if name == "tree":
        return "unix-tree"
    return name


def fetch(db: sqlite3.Connection, rid: str, out: Path) -> list[str]:
    row = db.execute("SELECT family, source_package, packaging_repository, packaging_commit, "
                     "packaging_path, metadata_pointer, metadata_sha256 FROM records WHERE id=?",
                     (rid,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown record {rid}")
    family, name, repo, commit, path, pointer, pointer_sha256 = row
    dest = out / rid.replace("/", "_").replace(":", "_")
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    if family == "deb":
        try:
            dsc = get(pointer)
        except urllib.error.HTTPError:
            # Same file from the live archive, accepted only if its digest matches.
            dsc = get("http://deb.debian.org/debian/" + pointer.split("/debian/", 2)[-1].split("/", 1)[-1])
        if pointer_sha256 and hashlib.sha256(dsc).hexdigest() != pointer_sha256:
            raise SystemExit(f"{rid}: .dsc digest does not match the record")
        (dest / Path(pointer).name).write_bytes(dsc)
        written.append(Path(pointer).name)
        for art_name, url in db.execute(
                "SELECT a.name, a.url FROM artifacts a JOIN records r ON r.id = a.record_id "
                "WHERE a.record_id=? AND (a.role = 'packaging' OR (r.native = 1 AND a.role = 'primary'))",
                (rid,)):
            (dest / art_name).write_bytes(get(url))
            written.append(art_name)
    elif family == "rpm":
        subprocess.run(["git", "init", "-q", str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", repo, commit], check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"], check=True)
        written = sorted(p.name for p in dest.iterdir() if p.name != ".git")
    elif family == "pkgbuild":
        project = arch_project(name)
        url = (f"https://gitlab.archlinux.org/archlinux/packaging/packages/{project}/-/archive/"
               f"{commit}/{project}-{commit}.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(get(url))) as t:
            t.extractall(dest, filter="data")
        written = [str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()]
    elif family == "ebuild":
        raw = f"https://raw.githubusercontent.com/gentoo-mirror/gentoo/{commit}"
        (dest / Path(path).name).write_bytes(get(f"{raw}/{path}"))
        written.append(Path(path).name)
        # The package's files/ directory (patches, init scripts) at the same commit.
        files_dir = str(Path(path).parent / "files")
        api = "https://api.github.com/repos/gentoo-mirror/gentoo/contents/{}?ref=" + commit
        try:
            stack = [json.loads(get(api.format(files_dir)))]
        except urllib.error.HTTPError as exc:
            stack = []
            if exc.code != 404:
                written.append(f"files/ not retrieved (GitHub API HTTP {exc.code})")
        while stack:
            for e in stack.pop():
                if e["type"] == "dir":
                    stack.append(json.loads(get(api.format(e["path"]))))
                elif e["type"] == "file":
                    rel = Path("files") / Path(e["path"]).relative_to(files_dir)
                    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    (dest / rel).write_bytes(get(f"{raw}/{e['path']}"))
                    written.append(str(rel))
    (dest / "POINTER.json").write_text(json.dumps(
        {"record": rid, "repository": repo, "commit": commit, "path": path,
         "metadata_pointer": pointer}, indent=2) + "\n")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    db = sqlite3.connect(args.db)
    for rid in args.records:
        print(rid, "->", fetch(db, rid, args.out))


if __name__ == "__main__":
    main()
