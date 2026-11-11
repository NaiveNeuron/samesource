#!/usr/bin/env python3
"""Draw the link-interpretation audit sample and write a blank labelling sheet.

30 primary-primary links per populated family pair (all if fewer), drawn
uniformly with a seed equal to the SHA-256 of links.jsonl, so anyone can
redraw the same sample from the release. Each item carries both records'
names, versions, artifact names, URLs, and packaging pointers. Raters fill
`verdict` with one of: same_primary_archive, vendored_or_bundled,
component_or_split, different_product, unclear, plus a one-line reason.
Output: evidence/scale/audit-sheet.json
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
PER_PAIR = 30


def main() -> None:
    records, artifacts = {}, {}
    for fam in ("deb", "rpm", "pkgbuild", "ebuild"):
        for line in (SCALE / f"{fam}.jsonl").open():
            r = json.loads(line)
            records[r["id"]] = r
            for a in r["artifacts"]:
                artifacts[a["id"]] = (r, a)
    seed = hashlib.sha256((SCALE / "links.jsonl").read_bytes()).hexdigest()
    by_pair = defaultdict(list)
    for line in (SCALE / "links.jsonl").open():
        link = json.loads(line)
        if link["roles"] != ["primary", "primary"]:
            continue
        fa, fb = artifacts[link["a"]][0]["family"], artifacts[link["b"]][0]["family"]
        by_pair["-".join(sorted([fa, fb]))].append(link)
    rng = random.Random(int(seed, 16))
    items = []
    for pair in sorted(by_pair):
        links = sorted(by_pair[pair], key=lambda l: (l["a"], l["b"]))
        chosen = links if len(links) <= PER_PAIR else rng.sample(links, PER_PAIR)
        for link in chosen:
            side = []
            for key in ("a", "b"):
                r, a = artifacts[link[key]]
                side.append({"record": r["id"], "family": r["family"], "source_package": r["source_package"],
                             "version": r.get("version"), "artifact": a["name"], "url": a.get("url"),
                             "role_evidence": a.get("role_evidence"), "packaging": r["packaging"]})
            items.append({"pair": pair, "link": [link["a"], link["b"]], "algorithms": link["algorithms"],
                          "evidence": link.get("evidence"), "sides": side,
                          "verdict": None, "reason": None})
    sheet = {"schema": "pkgbridge-scale-audit/1", "seed_sha256_of_links": seed,
             "per_pair": PER_PAIR, "population_by_pair": {p: len(v) for p, v in by_pair.items()},
             "verdicts": ["same_primary_archive", "vendored_or_bundled", "component_or_split",
                          "different_product", "unclear"],
             "items": items}
    (SCALE / "audit-sheet.json").write_text(json.dumps(sheet, indent=2) + "\n")
    print(len(items), "items;", sheet["population_by_pair"])


if __name__ == "__main__":
    main()
