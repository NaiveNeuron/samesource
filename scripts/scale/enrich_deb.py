#!/usr/bin/env python3
"""Bridge Debian's SHA-256-only digests to SHA-512/BLAKE2b by streaming downloads.

Debian declares only SHA-256 (and MD5) for .orig tarballs, while Fedora and
Gentoo declare SHA-512 and Gentoo BLAKE2b. For a deterministic candidate set,
stream each Debian primary/component archive from deb.debian.org, verify its
declared SHA-256 and size, and record the observed SHA-512 and BLAKE2b. Files
are hashed in memory and never written to disk.

Candidates (name-independent first):
  size   the archive's size equals the size of some Gentoo primary/component
         distfile (Gentoo declares sizes)
  name   the record's source name and upstream version match a Fedora or Arch
         record, so the pair has no common digest algorithm otherwise
Output: evidence/scale/deb-enrichment.jsonl and deb-enrichment-summary.json.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
AGENT = {"User-Agent": "pkgbridge-scale (research dataset)"}


def norm(name: str) -> str:
    return name.split("/")[-1].lower()


def nver(v: str | None) -> str | None:
    return re.sub(r"[._\-+~]+", ".", v.lower().lstrip("v")) if v else None


def stream_hashes(url: str) -> dict:
    for attempt in range(4):
        try:
            h256, h512, hb2 = hashlib.sha256(), hashlib.sha512(), hashlib.blake2b()
            size = 0
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=300) as r:
                while chunk := r.read(1 << 20):
                    size += len(chunk)
                    h256.update(chunk); h512.update(chunk); hb2.update(chunk)
            return {"size": size, "sha256": h256.hexdigest(), "sha512": h512.hexdigest(),
                    "blake2b": hb2.hexdigest()}
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                return {"error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 3:
                return {"error": type(exc).__name__}
        time.sleep(5 * (attempt + 1))
    return {"error": "exhausted"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--limit", type=int, help="process only the first N candidates (testing)")
    args = parser.parse_args()
    gentoo_sizes = set()
    for line in (SCALE / "ebuild.jsonl").open():
        for a in json.loads(line)["artifacts"]:
            if a.get("size") and a["role"] in ("primary", "component") and a["hash_domain"] == "file_bytes":
                gentoo_sizes.add(a["size"])
    keys = set()
    for fam in ("rpm", "pkgbuild"):
        path = SCALE / f"{fam}.jsonl"
        if path.exists():
            for line in path.open():
                r = json.loads(line)
                keys.add((norm(r["source_package"]), nver(r.get("upstream_version"))))
    candidates = []
    for line in (SCALE / "deb.jsonl").open():
        r = json.loads(line)
        by_name = (norm(r["source_package"]), nver(r.get("upstream_version"))) in keys
        for a in r["artifacts"]:
            if a["role"] not in ("primary", "component") or not a.get("url"):
                continue
            reasons = [x for x, ok in (("size", a.get("size") in gentoo_sizes), ("name", by_name)) if ok]
            if reasons:
                candidates.append((a, reasons))
    candidates.sort(key=lambda c: c[0]["id"])
    if args.limit:
        candidates = candidates[: args.limit]

    out_path = SCALE / "deb-enrichment.jsonl"
    done = {}
    if out_path.exists():
        for line in out_path.open():
            row = json.loads(line)
            if "error" not in row:
                done[row["artifact_id"]] = row
    todo = [c for c in candidates if c[0]["id"] not in done]

    def work(item):
        art, reasons = item
        got = stream_hashes(art["url"])
        row = {"artifact_id": art["id"], "url": art["url"], "candidate_reasons": reasons, **got}
        if "sha256" in got:
            row["declared_sha256_matches"] = got["sha256"] == (art["declared"].get("sha256") or "").lower()
            row["declared_size_matches"] = got["size"] == art.get("size")
        return row

    with out_path.open("a") as out, ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for row in pool.map(work, todo):
            out.write(json.dumps(row, sort_keys=True) + "\n")
            out.flush()
            done[row["artifact_id"]] = row
    rows = [json.loads(l) for l in out_path.open()]
    latest = {r["artifact_id"]: r for r in rows}
    summary = {"candidates": len(candidates),
               "hashed": sum("sha512" in r for r in latest.values()),
               "errors": sum("error" in r for r in latest.values()),
               "declared_sha256_mismatch": sum(r.get("declared_sha256_matches") is False
                                               for r in latest.values()),
               "bytes_streamed": sum(r.get("size", 0) for r in latest.values())}
    (SCALE / "deb-enrichment-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
