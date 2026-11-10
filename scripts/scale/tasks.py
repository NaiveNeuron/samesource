#!/usr/bin/env python3
"""Directional cross-family task manifests from byte-linked program groups.

A task pairs a source recipe (one family) with a held-out target recipe (another
family) for the same program group. Tiers:
  byte_complete  every primary artifact of both records is linked to a primary
                 artifact of the other record by identical declared bytes
  byte_partial   at least one primary-primary byte link, but not all
Splits are assigned per group (all directions of a group share a split) from
a hash of the group's sorted record ids, so they are stable across releases.
Public target recipes may have been seen in model training; tiers control the
upstream input, not contamination.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import itertools
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
FAMILIES = ["deb", "rpm", "pkgbuild", "ebuild"]


def split_of(group_key: str) -> str:
    bucket = int(hashlib.sha256(group_key.encode()).hexdigest(), 16) % 10
    return "test" if bucket < 2 else "dev" if bucket < 3 else "train"


def main() -> None:
    records = {}
    for fam in FAMILIES:
        for line in (SCALE / f"{fam}.jsonl").open():
            r = json.loads(line)
            records[r["id"]] = r
    primary_owner = {a["id"]: r["id"] for r in records.values() for a in r["artifacts"]
                     if a["role"] == "primary"}
    linked = defaultdict(set)          # primary artifact -> primary artifacts with same bytes
    for line in (SCALE / "links.jsonl").open():
        link = json.loads(line)
        if link["a"] in primary_owner and link["b"] in primary_owner:
            linked[link["a"]].add(link["b"])
            linked[link["b"]].add(link["a"])

    def covered(src: dict, dst: dict) -> tuple[int, int]:
        prim_s = [a["id"] for a in src["artifacts"] if a["role"] == "primary"]
        prim_d = {a["id"] for a in dst["artifacts"] if a["role"] == "primary"}
        hit_s = sum(bool(linked[a] & prim_d) for a in prim_s)
        hit_d = sum(bool(linked[b] & set(prim_s)) for b in prim_d)
        return hit_s + hit_d, len(prim_s) + len(prim_d)

    tasks = []
    for line in (SCALE / "groups.jsonl").open():
        group = json.loads(line)
        key = "|".join(group["records"])
        split = split_of(key)
        for sid, tid in itertools.permutations(group["records"], 2):
            src, dst = records[sid], records[tid]
            if src["family"] == dst["family"]:
                continue
            hits, total = covered(src, dst)
            if hits == 0:
                continue
            tasks.append({
                "task_id": f"{sid}->{tid}", "group": key, "split": split,
                "source": {"id": sid, "family": src["family"], "packaging": src["packaging"]},
                "target": {"id": tid, "family": dst["family"], "packaging": dst["packaging"]},
                "tier": "byte_complete" if hits == total else "byte_partial",
                "same_upstream_version_string": (src.get("upstream_version") or "").lower()
                                                 == (dst.get("upstream_version") or "").lower(),
            })
    tasks.sort(key=lambda t: t["task_id"])
    (SCALE / "tasks.jsonl").write_text("".join(json.dumps(t, sort_keys=True) + "\n" for t in tasks))
    counts = defaultdict(lambda: defaultdict(int))
    for t in tasks:
        counts[f"{t['source']['family']}->{t['target']['family']}"][t["tier"]] += 1
    summary = {"tasks": len(tasks),
               "by_direction": {k: dict(v) for k, v in sorted(counts.items())},
               "by_split": {s: sum(t["split"] == s for t in tasks) for s in ("train", "dev", "test")}}
    (SCALE / "tasks-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
