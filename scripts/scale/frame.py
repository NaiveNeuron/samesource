#!/usr/bin/env python3
"""Pin the frame for the scaled dataset: metadata files and repository commits.

Downloads each distribution's source-package metadata for one date into
CACHE/frame/, clones the two repositories that index packaging state at a
recorded commit, and writes evidence/scale/frame.json with every URL, commit,
and SHA-256. Later steps read only these pinned inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache" / "pkgbridge" / "scale"
AGENT = {"User-Agent": "pkgbridge-scale (research dataset; contact via repository)"}


def download(url: str, dest: Path) -> str:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(6):  # snapshot.debian.org answers 5xx intermittently
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT),
                                            timeout=300) as r:
                    data = r.read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt == 5:
                    raise
                time.sleep(10 * (attempt + 1))
        dest.write_bytes(data)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def shallow_clone(url: str, commit: str, dest: Path) -> None:
    if not dest.exists():
        subprocess.run(["git", "init", "-q", str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", url, commit],
                       check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"], check=True)
    head = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
    if head != commit:
        raise SystemExit(f"{dest} is at {head}, expected {commit}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-11-05")
    parser.add_argument("--fedora-compose", default="Fedora-Rawhide-20261105.n.0")
    parser.add_argument("--arch-state-commit", required=True)
    parser.add_argument("--gentoo-commit", required=True,
                        help="commit of github.com/gentoo-mirror/gentoo (carries metadata/md5-cache)")
    args = parser.parse_args()
    y, m, d = args.date.split("-")
    stamp = f"{y}{m}{d}T000000Z"
    frame_dir = CACHE / "frame"
    compose = f"https://kojipkgs.fedoraproject.org/compose/rawhide/{args.fedora_compose}"
    repomd_url = f"{compose}/compose/Everything/source/tree/repodata/repomd.xml"
    download(repomd_url, frame_dir / "fedora-repomd.xml")
    repomd = (frame_dir / "fedora-repomd.xml").read_text()
    primary = repomd.split('href="repodata/')[1].split('"')[0]
    for candidate in repomd.split('href="repodata/')[1:]:
        name = candidate.split('"')[0]
        if name.endswith("primary.xml.zst"):
            primary = name
    files = {
        "debian_sources": (f"https://snapshot.debian.org/archive/debian/{stamp}/dists/unstable/"
                           "main/source/Sources.xz", frame_dir / "debian-Sources.xz"),
        "fedora_primary": (f"{compose}/compose/Everything/source/tree/repodata/{primary}",
                           frame_dir / "fedora-primary.xml.zst"),
        "arch_core_db": (f"https://archive.archlinux.org/repos/{y}/{m}/{d}/core/os/x86_64/core.db",
                         frame_dir / "arch-core.db"),
        "arch_extra_db": (f"https://archive.archlinux.org/repos/{y}/{m}/{d}/extra/os/x86_64/extra.db",
                          frame_dir / "arch-extra.db"),
    }
    record = {"schema": "pkgbridge-scale-frame/1", "date": args.date,
              "debian_snapshot": stamp, "fedora_compose": args.fedora_compose, "files": {}}
    for key, (url, dest) in files.items():
        record["files"][key] = {"url": url, "path": str(dest.relative_to(CACHE)),
                                "sha256": download(url, dest)}
    repos = {
        "arch_state": ("https://gitlab.archlinux.org/archlinux/packaging/state.git",
                       args.arch_state_commit, CACHE / "arch-state"),
        "gentoo": ("https://github.com/gentoo-mirror/gentoo.git", args.gentoo_commit,
                   CACHE / "gentoo-mirror"),
    }
    record["repositories"] = {}
    for key, (url, commit, dest) in repos.items():
        shallow_clone(url, commit, dest)
        record["repositories"][key] = {"url": url, "commit": commit}
    out = PROJECT / "evidence" / "scale" / "frame.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
