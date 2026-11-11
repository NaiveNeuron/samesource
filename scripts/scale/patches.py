#!/usr/bin/env python3
"""Use: declared downstream patches for packages that start from identical bytes.

For every record in a byte-linked group, count the patches its packaging
declares, with the evidence used:
  deb       entries in debian/patches/series inside the pinned .debian.tar.*
  rpm       Patch tags in the spec at the Koji-pinned dist-git commit
  pkgbuild  local or downloaded *.patch / *.diff sources in .SRCINFO
  ebuild    *.patch / *.diff names in the ebuild text plus patch_bundle artifacts
Counts are declarations, not applied changes; a patch's absence in one
distribution and presence in another is a divergence in declared inputs only.
Downloads are cached under ~/.cache/pkgbridge/scale/raw/patches/.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import re
import tarfile
import time
import urllib.error
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
CACHE = Path.home() / ".cache" / "pkgbridge" / "scale"
RAW = CACHE / "raw" / "patches"
AGENT = {"User-Agent": "pkgbridge-scale (research dataset)"}
PATCH_NAME = re.compile(r"[\w.+@-]+\.(?:patch|diff)\b")


def fetch(url: str, dest: Path) -> bytes | None:
    if dest.exists():
        return dest.read_bytes() or None
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=120) as r:
                data = r.read()
            dest.write_bytes(data)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                dest.write_bytes(b"")
                return None
            if exc.code < 500 or attempt == 3:
                return None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                return None
        time.sleep(3 * (attempt + 1))
    return None


def deb_patches(rec: dict) -> dict:
    tar = next((a for a in rec["artifacts"] if a["role"] == "packaging"), None)
    if tar is None or not tar.get("url"):
        return {"count": None, "evidence": "no packaging tarball"}
    if tar["name"].endswith(".diff.gz"):
        return {"count": None, "evidence": "1.0 diff.gz format; patches not separable"}
    data = fetch(tar["url"], RAW / "deb" / tar["name"])
    if not data:
        return {"count": None, "evidence": "packaging tarball unavailable"}
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        members = {m.name.removeprefix("./"): m for m in t.getmembers()}
        series = members.get("debian/patches/series")
        if series is None:
            return {"count": 0, "evidence": "no debian/patches/series"}
        lines = [l.split("#", 1)[0].strip() for l in
                 t.extractfile(series).read().decode(errors="replace").splitlines()]
    return {"count": sum(1 for l in lines if l), "evidence": "debian/patches/series"}


def rpm_patches(rec: dict) -> dict:
    p = rec["packaging"]
    if not p.get("commit"):
        return {"count": None, "evidence": "no pinned commit"}
    name = rec["source_package"]
    url = f"https://src.fedoraproject.org/rpms/{name}/raw/{p['commit']}/f/{name}.spec"
    data = fetch(url, RAW / "rpm" / f"{name}-{p['commit']}.spec")
    if not data:
        return {"count": None, "evidence": "spec unavailable"}
    tags = re.findall(r"^Patch\d*\s*:", data.decode(errors="replace"), re.M)
    return {"count": len(tags), "evidence": "Patch tags in spec"}


def arch_patches(rec: dict) -> dict:
    declared = [a for a in rec["artifacts"] if a.get("role") != "signature"
                and re.search(r"\.(patch|diff)$", a.get("name") or "")]
    return {"count": len(declared), "evidence": "local or downloaded .patch/.diff sources in .SRCINFO"}


def ebuild_patches(rec: dict) -> dict:
    path = CACHE / "gentoo-mirror" / rec["packaging"]["path"]
    if not path.exists():
        return {"count": None, "evidence": "ebuild file missing"}
    names = set(PATCH_NAME.findall(path.read_text(errors="replace")))
    bundles = [a for a in rec["artifacts"] if a["role"] == "patch_bundle"]
    return {"count": len(names) + len(bundles),
            "evidence": "patch names in ebuild text + patch_bundle artifacts"}


COUNTERS = {"deb": deb_patches, "rpm": rpm_patches, "pkgbuild": arch_patches, "ebuild": ebuild_patches}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()
    records = {}
    for fam in COUNTERS:
        for line in (SCALE / f"{fam}.jsonl").open():
            r = json.loads(line)
            records[r["id"]] = r
    groups = [json.loads(l) for l in (SCALE / "groups.jsonl").open()]
    needed = sorted({rid for g in groups for rid in g["records"]})
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = dict(zip(needed, pool.map(lambda rid: COUNTERS[records[rid]["family"]](records[rid]),
                                            needed)))
    rows, outcome = [], {"all_unpatched": 0, "all_patched": 0, "mixed": 0, "incomplete": 0}
    for g in groups:
        counts = {rid: results[rid]["count"] for rid in g["records"]}
        if any(c is None for c in counts.values()):
            state = "incomplete"
        elif all(c == 0 for c in counts.values()):
            state = "all_unpatched"
        elif all(c > 0 for c in counts.values()):
            state = "all_patched"
        else:
            state = "mixed"
        outcome[state] += 1
        rows.append({"records": g["records"], "families": g["families"], "patch_counts": counts,
                     "state": state})
    per_family = {}
    for fam in COUNTERS:
        vals = [results[r]["count"] for r in needed if records[r]["family"] == fam]
        known = [v for v in vals if v is not None]
        per_family[fam] = {"records": len(vals), "known": len(known),
                           "patched": sum(v > 0 for v in known),
                           "median_patches": sorted(known)[len(known) // 2] if known else None}
    out = {"schema": "pkgbridge-scale-patches/1",
           "interpretation": "Declared patch counts per record in byte-linked groups; declarations, "
                             "not applied changes.",
           "groups": len(groups), "outcome": outcome, "per_family": per_family,
           "records": {rid: results[rid] for rid in needed}, "rows": rows}
    (SCALE / "patches.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({k: out[k] for k in ("groups", "outcome", "per_family")}, indent=2))


if __name__ == "__main__":
    main()
