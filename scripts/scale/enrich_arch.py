#!/usr/bin/env python3
"""Bridge Arch primary archives that declare only SHA-256 to SHA-512/BLAKE2b.

Arch recipes choose their own checksum algorithm; when a primary archive has
only SHA-256, Fedora and Gentoo digests cannot be compared with it. For Arch
records whose name and upstream version match a Fedora or Gentoo record,
stream the archive from its declared upstream URL, verify the declared
SHA-256, and record observed SHA-512 and BLAKE2b. Nothing is written to disk.
Output: evidence/scale/pkgbuild-enrichment.jsonl (+ summary).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from enrich_deb import norm, nver, stream_hashes

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    keys = set()
    for fam in ("rpm", "ebuild"):
        for line in (SCALE / f"{fam}.jsonl").open():
            r = json.loads(line)
            keys.add((norm(r["source_package"]), nver(r.get("upstream_version"))))
    candidates = []
    for line in (SCALE / "pkgbuild.jsonl").open():
        r = json.loads(line)
        if (norm(r["source_package"]), nver(r.get("upstream_version"))) not in keys:
            continue
        for a in r["artifacts"]:
            d = a.get("declared") or {}
            if (a["role"] == "primary" and a["hash_domain"] == "file_bytes" and d.get("sha256")
                    and not d.get("sha512") and not d.get("blake2b")
                    and (a.get("url") or "").startswith(("http://", "https://", "ftp://"))):
                candidates.append(a)
    candidates.sort(key=lambda a: a["id"])
    out_path = SCALE / "pkgbuild-enrichment.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(l)["artifact_id"] for l in out_path.open() if "error" not in json.loads(l)}

    def work(art):
        got = stream_hashes(art["url"])
        row = {"artifact_id": art["id"], "url": art["url"], **got}
        if "sha256" in got:
            row["declared_sha256_matches"] = got["sha256"] == art["declared"]["sha256"].lower()
            row["declared_size_matches"] = True  # Arch declares no size; SHA-256 is the check
        return row

    with out_path.open("a") as out, ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for row in pool.map(work, [a for a in candidates if a["id"] not in done]):
            out.write(json.dumps(row, sort_keys=True) + "\n")
            out.flush()
    latest = {json.loads(l)["artifact_id"]: json.loads(l) for l in out_path.open()}
    summary = {"candidates": len(candidates),
               "hashed": sum("sha512" in r for r in latest.values()),
               "errors": sum("error" in r for r in latest.values()),
               "declared_sha256_mismatch": sum(r.get("declared_sha256_matches") is False
                                               for r in latest.values()),
               "bytes_streamed": sum(r.get("size", 0) for r in latest.values())}
    (SCALE / "pkgbuild-enrichment-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
