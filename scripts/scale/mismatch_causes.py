#!/usr/bin/env python3
"""Why do same-name, same-version package pairs build from different bytes?

Re-derives the pairs that census.json counts as different_bytes or
different_bytes_by_size and assigns each the first matching cause, from
literal evidence only:
  debian_repack       a Debian side whose version or role evidence marks a repack
  crate_vs_archive    one side's primary is a .crate, the other's is not
  forge_archive       exactly one side downloads a forge-generated archive
                      (URL path /archive/, /-/archive/, codeload, /tarball/)
  compression_only    archive basenames equal once the compression suffix and
                      Debian's _VERSION.orig naming are normalised
  other               none of the above
Output: evidence/scale/mismatch-causes.json
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import re

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
ALGORITHMS = ["sha512", "blake2b", "sha256"]
FORGE = re.compile(r"/archive/|/-/archive/|codeload\.|/tarball/|/zipball/")
COMPRESSION = re.compile(r"\.(tar\.(gz|xz|bz2|zst|lz|lzma)|tgz|tbz2?|txz|zip)$")


def nname(n: str) -> str:
    return n.split("/")[-1].lower()


def nver(v):
    return re.sub(r"[._\-+~]+", ".", v.lower().lstrip("v")) if v else None


def stem(art_name: str, version: str | None) -> str:
    s = COMPRESSION.sub("", art_name.lower())
    s = re.sub(r"\.orig(-[\w.-]+)?$", "", s)
    s = s.replace("_", "-")
    return re.sub(r"[+~](dfsg|ds|repack)\d*$", "", s)


def main() -> None:
    records = {}
    for fam in ("deb", "rpm", "pkgbuild", "ebuild"):
        for line in (SCALE / f"{fam}.jsonl").open():
            r = json.loads(line)
            records[r["id"]] = r
    observed = {}
    for name in ("deb-enrichment.jsonl", "pkgbuild-enrichment.jsonl"):
        for line in (SCALE / name).open():
            row = json.loads(line)
            if row.get("declared_sha256_matches") and row.get("declared_size_matches"):
                observed[row["artifact_id"]] = {"sha512": row["sha512"], "blake2b": row["blake2b"]}
    linked = set()
    for line in (SCALE / "links.jsonl").open():
        l = json.loads(line)
        if l["roles"] == ["primary", "primary"]:
            ra, rb = l["a"].split("#")[0], l["b"].split("#")[0]
            linked |= {(ra, rb), (rb, ra)}

    def digests(a):
        d = {k: v.lower() for k, v in (a.get("declared") or {}).items() if v and k in ALGORITHMS}
        d.update({k: v for k, v in observed.get(a["id"], {}).items() if k not in d})
        return d

    keyed = defaultdict(list)
    for r in records.values():
        if not r.get("live_vcs"):
            keyed[(nname(r["source_package"]), nver(r.get("upstream_version")))].append(r)
    causes = defaultdict(Counter)
    examples = defaultdict(list)
    for (name, version), rs in keyed.items():
        if version is None:
            continue
        for ra, rb in itertools.combinations(rs, 2):
            if ra["family"] == rb["family"] or (ra["id"], rb["id"]) in linked:
                continue
            pa = [a for a in ra["artifacts"] if a["role"] == "primary"]
            pb = [a for a in rb["artifacts"] if a["role"] == "primary"]
            if not pa or not pb or any(a["hash_domain"] != "file_bytes" for a in pa + pb):
                continue
            common = {k for a in pa for k in digests(a)} & {k for b in pb for k in digests(b)}
            sizes_a = {a.get("size") for a in pa} - {None}
            sizes_b = {b.get("size") for b in pb} - {None}
            if not common and not (sizes_a and sizes_b and not sizes_a & sizes_b):
                continue
            sides = {ra["family"]: (ra, pa), rb["family"]: (rb, pb)}
            deb = sides.get("deb")
            if deb and (re.search(r"[+~.](dfsg|ds|repack)", deb[0].get("version") or "")
                        or any("repack" in (a.get("role_evidence") or "") for a in deb[1])):
                cause = "debian_repack"
            elif (any(a.get("kind") == "crate" for a in pa)) != (any(b.get("kind") == "crate" for b in pb)):
                cause = "crate_vs_archive"
            elif bool(any(FORGE.search(a.get("url") or "") for a in pa)) != \
                    bool(any(FORGE.search(b.get("url") or "") for b in pb)):
                cause = "forge_archive"
            elif {stem(a["name"], version) for a in pa} & {stem(b["name"], version) for b in pb}:
                cause = "compression_or_rename_only"
            else:
                cause = "other"
            pair = "-".join(sorted([ra["family"], rb["family"]]))
            causes[pair][cause] += 1
            examples[cause].append([ra["id"], rb["id"]])
    total = Counter()
    for c in causes.values():
        total.update(c)
    sample = {c: sorted(v, key=lambda p: hashlib.sha256("|".join(p).encode()).hexdigest())[:10]
              for c, v in examples.items()}
    out = {"schema": "pkgbridge-scale-mismatch-causes/1", "total": sum(total.values()),
           "by_cause": dict(total.most_common()), "by_pair": {k: dict(v) for k, v in sorted(causes.items())},
           "examples": sample}
    (SCALE / "mismatch-causes.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in ("total", "by_cause")}, indent=2))


if __name__ == "__main__":
    main()
