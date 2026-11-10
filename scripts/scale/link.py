#!/usr/bin/env python3
"""Link package revisions across families by identical declared archive digests.

Inputs: evidence/scale/{deb,rpm,pkgbuild,ebuild}.jsonl (v1 schema).
Outputs:
  evidence/scale/links.jsonl   one same_file_bytes assertion per artifact pair
  evidence/scale/groups.jsonl  program groups: connected components over
                               links between *primary* artifacts only
  evidence/scale/census.json   counts, plus a name-and-version census that
                               explains why candidate pairs do or do not link

A link is a byte fact about two declared digests in the same algorithm. It is
not a claim that the two packages build the same complete input or product.
Signatures, patches, packaging files and vendor bundles never join groups.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import itertools
import json
from pathlib import Path
import re

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
FAMILIES = ["deb", "rpm", "pkgbuild", "ebuild"]
ALGORITHMS = ["sha512", "blake2b", "sha256"]   # md5 is never used to link
GROUP_ROLES = {"primary"}


def norm_name(name: str) -> str:
    return name.split("/")[-1].lower()


def norm_version(version: str | None) -> str | None:
    return re.sub(r"[._\-+~]+", ".", version.lower().lstrip("v")) if version else None


def load(families: list[str]) -> dict[str, list[dict]]:
    return {f: [json.loads(line) for line in (SCALE / f"{f}.jsonl").open()] for f in families}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--families", default=",".join(FAMILIES),
                        help="comma-separated subset (for partial runs)")
    args = parser.parse_args()
    records = load(args.families.split(","))
    by_id = {r["id"]: r for rs in records.values() for r in rs}
    artifacts = {a["id"]: (r, a) for rs in records.values() for r in rs for a in r["artifacts"]}

    # Digests by source: what the distribution declared, plus digests we
    # observed by streaming a download whose declared SHA-256 and size matched.
    observed: dict[str, dict] = {}
    for enrichment in (SCALE / "deb-enrichment.jsonl", SCALE / "pkgbuild-enrichment.jsonl"):
        if not enrichment.exists():
            continue
        for line in enrichment.open():
            row = json.loads(line)
            if row.get("declared_sha256_matches") and row.get("declared_size_matches"):
                observed[row["artifact_id"]] = {"sha512": row["sha512"], "blake2b": row["blake2b"]}
    index: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for art_id, (rec, art) in artifacts.items():
        if art.get("hash_domain") != "file_bytes":
            continue
        for alg in ALGORITHMS:
            digest = (art.get("declared") or {}).get(alg)
            if digest:
                index[(alg, digest.lower())].append((art_id, "declared"))
            elif alg in observed.get(art_id, {}):
                index[(alg, observed[art_id][alg])].append((art_id, "observed_download"))

    links = {}
    for (alg, digest), entries in index.items():
        source = dict(entries)
        for a, b in itertools.combinations(sorted(source), 2):
            ra, rb = artifacts[a][0], artifacts[b][0]
            if ra["family"] == rb["family"]:
                continue
            key = (a, b)
            entry = links.setdefault(key, {"a": a, "b": b, "assertion": "same_file_bytes",
                                           "algorithms": [], "evidence": [], "status": "supported",
                                           "roles": [artifacts[a][1]["role"], artifacts[b][1]["role"]],
                                           "names": [artifacts[a][1]["name"], artifacts[b][1]["name"]]})
            entry["algorithms"].append(alg)
            entry["evidence"].append(f"{alg}:{source[a]}/{source[b]}")

    # Program groups: union-find over records joined by primary-primary links.
    parent = {rid: rid for rid in by_id}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for link in links.values():
        if set(link["roles"]) <= GROUP_ROLES:
            ra, rb = artifacts[link["a"]][0]["id"], artifacts[link["b"]][0]["id"]
            parent[find(ra)] = find(rb)
    members = defaultdict(list)
    for rid in by_id:
        members[find(rid)].append(rid)
    groups = []
    for rids in members.values():
        fams = sorted({by_id[r]["family"] for r in rids})
        if len(fams) < 2:
            continue
        groups.append({"records": sorted(rids), "families": fams,
                       "source_packages": sorted({f"{by_id[r]['family']}:{by_id[r]['source_package']}"
                                                  for r in rids})})
    groups.sort(key=lambda g: g["records"][0])

    # Name-and-version census: for same-name same-version record pairs, why (not) linked?
    linked_pairs = {(artifacts[l["a"]][0]["id"], artifacts[l["b"]][0]["id"]) for l in links.values()
                    if set(l["roles"]) <= GROUP_ROLES}
    linked_pairs |= {(b, a) for a, b in linked_pairs}
    keyed = defaultdict(list)
    for fam, rs in records.items():
        for r in rs:
            if r.get("live_vcs"):
                continue
            keyed[(norm_name(r["source_package"]), norm_version(r.get("upstream_version")))].append(r)
    census = defaultdict(Counter)
    for (name, version), rs in keyed.items():
        if version is None:
            continue
        for ra, rb in itertools.combinations(rs, 2):
            if ra["family"] == rb["family"]:
                continue
            pair = "-".join(sorted([ra["family"], rb["family"]]))
            if (ra["id"], rb["id"]) in linked_pairs:
                census[pair]["linked_same_bytes"] += 1
                continue
            pa = [a for a in ra["artifacts"] if a["role"] == "primary"]
            pb = [a for a in rb["artifacts"] if a["role"] == "primary"]
            if not pa or not pb:
                census[pair]["no_primary_artifact"] += 1
            elif any(a["hash_domain"] != "file_bytes" for a in pa + pb):
                census[pair]["vcs_source"] += 1
            else:
                def algs(arts):
                    return {alg for a in arts for alg in ALGORITHMS
                            if (a.get("declared") or {}).get(alg) or alg in observed.get(a["id"], {})}
                algs_a, algs_b = algs(pa), algs(pb)
                sizes_a = {a.get("size") for a in pa} - {None}
                sizes_b = {b.get("size") for b in pb} - {None}
                if algs_a & algs_b:
                    census[pair]["different_bytes"] += 1
                elif sizes_a and sizes_b and not sizes_a & sizes_b:
                    census[pair]["different_bytes_by_size"] += 1
                else:
                    census[pair]["no_common_algorithm"] += 1

    (SCALE / "links.jsonl").write_text("".join(json.dumps(l, sort_keys=True) + "\n"
                                               for l in sorted(links.values(), key=lambda l: (l["a"], l["b"]))))
    (SCALE / "groups.jsonl").write_text("".join(json.dumps(g, sort_keys=True) + "\n" for g in groups))
    role_pairs = Counter("+".join(sorted(l["roles"])) for l in links.values())
    fam_pairs = Counter("-".join(sorted([artifacts[l["a"]][0]["family"], artifacts[l["b"]][0]["family"]]))
                        for l in links.values() if set(l["roles"]) <= GROUP_ROLES)
    summary = {
        "schema": "pkgbridge-scale-census/1",
        "records": {f: len(rs) for f, rs in records.items()},
        "links_total": len(links),
        "links_by_roles": dict(role_pairs),
        "primary_links_by_family_pair": dict(fam_pairs),
        "groups_spanning_families": len(groups),
        "groups_by_family_count": dict(Counter(len(g["families"]) for g in groups)),
        "groups_by_family_set": dict(Counter("+".join(g["families"]) for g in groups)),
        "name_version_census": {k: dict(v) for k, v in sorted(census.items())},
    }
    (SCALE / "census.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
