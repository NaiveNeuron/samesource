#!/usr/bin/env python3
"""Where do package names and upstream bytes disagree?

For every cross-family record pair inside a byte-linked group, record whether
the normalised source-package names agree. For records that share a name and
upstream version string but are not byte-linked, record why (via census.json).
Also checks the six misidentified PkgBridge seed views: none of them may be
byte-linked to the program its catalog name claims.
Output: evidence/scale/names.json
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
PREFIXES = ("python-", "python3-", "py-", "rust-", "golang-", "perl-", "ruby-", "rubygem-",
            "php-", "lua-", "ghc-", "haskell-", "node-", "nodejs-", "ocaml-", "r-", "lib")


def base(name: str) -> str:
    name = name.split("/")[-1].lower()
    return re.sub(r"[-_.]", "", name)


def stripped(name: str) -> str:
    n = name.split("/")[-1].lower()
    for p in PREFIXES:
        if n.startswith(p) and len(n) > len(p) + 1:
            n = n[len(p):]
            break
    return re.sub(r"[-_.]", "", n)


def main() -> None:
    records = {}
    for fam in ("deb", "rpm", "pkgbuild", "ebuild"):
        path = SCALE / f"{fam}.jsonl"
        if path.exists():
            for line in path.open():
                r = json.loads(line)
                records[r["id"]] = r
    groups = [json.loads(l) for l in (SCALE / "groups.jsonl").open()]
    pair_kind = defaultdict(Counter)
    examples = defaultdict(list)
    for g in groups:
        for a, b in itertools.combinations(g["records"], 2):
            ra, rb = records[a], records[b]
            if ra["family"] == rb["family"]:
                continue
            fam = "-".join(sorted([ra["family"], rb["family"]]))
            na, nb = ra["source_package"], rb["source_package"]
            if base(na) == base(nb):
                kind = "same_name"
            elif stripped(na) == stripped(nb):
                kind = "same_after_ecosystem_prefix"
            else:
                kind = "different_name"
                examples[fam].append([a, b])
            pair_kind[fam][kind] += 1
    sample = {}
    for fam, pairs in examples.items():
        pairs.sort(key=lambda p: hashlib.sha256("|".join(p).encode()).hexdigest())
        sample[fam] = pairs[:15]

    seed = json.loads((PROJECT / "evidence" / "identity-adjudication.json").read_text())
    member = {rid: i for i, g in enumerate(groups) for rid in g["records"]}
    regression = []
    family_map = {"nix": None, "deb": "deb", "rpm": "rpm"}
    for j in seed["rejected_joins"]:
        fam = family_map.get(j["family"])
        if fam is None:
            regression.append({**j, "result": "family not in scaled dataset (Nix)"})
            continue
        wrong = [r for r in records.values() if r["family"] == fam
                 and base(r["source_package"]) == base(j["program"])]
        others = [r for r in records.values() if r["family"] != fam
                  and base(r["source_package"]) == base(j["program"])]
        joined = any(member.get(w["id"]) is not None and member.get(w["id"]) == member.get(o["id"])
                     for w in wrong for o in others)
        regression.append({**j, "records_in_family": [w["id"] for w in wrong],
                           "byte_linked_to_same_name_elsewhere": joined})
    out = {"schema": "pkgbridge-scale-names/1",
           "pairs_in_groups_by_name_relation": {k: dict(v) for k, v in sorted(pair_kind.items())},
           "different_name_examples": sample,
           "seed_regression": regression}
    (SCALE / "names.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in ("pairs_in_groups_by_name_relation",)}, indent=2))
    for r in regression:
        print(r["program"], r["family"], r.get("byte_linked_to_same_name_elsewhere", r.get("result")))


if __name__ == "__main__":
    main()
