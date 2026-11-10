#!/usr/bin/env python3
"""Combine the two independent audit label files into counts, agreement, and kappa.

Inputs: evidence/scale/audit-sheet.json, audit-rater-a.json, audit-rater-b.json.
Output: evidence/scale/audit-summary.json. Both raters are language-model agents
working blind to each other; audit-human-*.csv files hold human labels of the same items.
"""

from __future__ import annotations

from collections import Counter
import csv
import json
import math
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 4), round(centre + half, 4))


def main() -> None:
    sheet = json.loads((SCALE / "audit-sheet.json").read_text())
    a = {tuple(i["link"]): i["verdict"] for i in json.loads((SCALE / "audit-rater-a.json").read_text())["items"]}
    b = {tuple(i["link"]): i["verdict"] for i in json.loads((SCALE / "audit-rater-b.json").read_text())["items"]}
    items = [tuple(i["link"]) for i in sheet["items"]]
    assert set(items) == set(a) == set(b), "raters must label exactly the sheet items"
    n = len(items)
    agree = sum(a[i] == b[i] for i in items)
    ca, cb = Counter(a[i] for i in items), Counter(b[i] for i in items)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    kappa = (agree / n - expected) / (1 - expected) if expected < 1 else 1.0
    both_same = sum(a[i] == b[i] == "same_primary_archive" for i in items)
    wrong = sum(a[i] == b[i] and a[i] in ("different_product", "vendored_or_bundled") for i in items)
    pair_of = {tuple(i["link"]): i["pair"] for i in sheet["items"]}
    per_pair = {}
    for pair, population in sheet["population_by_pair"].items():
        its = [i for i in items if pair_of[i] == pair]
        k = sum(a[i] == b[i] == "same_primary_archive" for i in its)
        per_pair[pair] = {"sampled": len(its), "population": population, "same_primary_archive": k,
                          "wilson95": wilson(k, len(its))}
    total_pop = sum(v["population"] for v in per_pair.values())
    weighted = sum(v["population"] * v["same_primary_archive"] / v["sampled"]
                   for v in per_pair.values()) / total_pop
    # Stratified variance (finite-population correction omitted; conservative).
    var = sum((v["population"] / total_pop) ** 2 *
              (p := v["same_primary_archive"] / v["sampled"]) * (1 - p) / max(v["sampled"] - 1, 1)
              for v in per_pair.values())
    humans = {}
    for path in sorted(SCALE.glob("audit-human-*.csv")):
        rows = list(csv.DictReader(path.open()))
        h = {(r["link_a"], r["link_b"]): r["verdict"] for r in rows if r["verdict"]}
        if set(h) != set(items):
            raise SystemExit(f"{path.name}: labels {len(h)} of {n} items; a complete sheet is required")
        hc = Counter(h[i] for i in items)
        model_agree = sum(h[i] == a[i] == b[i] for i in items)
        exp = sum(hc[k] * ca[k] for k in set(hc) | set(ca)) / (n * n)
        humans[path.stem.removeprefix("audit-human-")] = {
            "verdicts": dict(hc),
            "agrees_with_both_models": model_agree,
            "cohen_kappa_vs_models": round((model_agree / n - exp) / (1 - exp), 3) if exp < 1 else 1.0,
            "wrong_product_or_vendored": sum(h[i] in ("different_product", "vendored_or_bundled") for i in items),
            "unclear": hc.get("unclear", 0),
            "disagreements": [{"link": list(i), "human": h[i], "models": a[i]} for i in items if h[i] != a[i]],
        }
    adjudication = SCALE / "audit-adjudication.json"
    if adjudication.exists():
        adj = json.loads(adjudication.read_text())
        h = humans[adj["rater"]]
        revised = {tuple(r["link"]): r["revised"] for r in adj["revisions"]}
        blind = {tuple(d["link"]): d["human"] for d in h["disagreements"]}
        for link, r in zip(revised, adj["revisions"]):
            if blind.get(link) != r["blind"]:
                raise SystemExit(f"adjudication does not match the blind label for {link}")
        still = [d for d in h["disagreements"] if revised.get(tuple(d["link"])) != d["models"]]
        h["after_discussion"] = {"revised": len(revised), "agrees_with_both_models": n - len(still),
                                 "remaining_disagreements": still}
    summary = {
        "schema": "pkgbridge-scale-audit-summary/1",
        "items": n, "per_pair_sample": sheet["per_pair"],
        "rater_a": dict(ca), "rater_b": dict(cb),
        "agreement": agree, "cohen_kappa": round(kappa, 3),
        "both_same_primary_archive": both_same,
        "same_primary_archive_wilson95_unweighted": wilson(both_same, n),
        "same_primary_archive_population_weighted": round(weighted, 4),
        "same_primary_archive_weighted_se": round(math.sqrt(var), 4),
        "by_pair": per_pair,
        "both_wrong_product_or_vendored": wrong,
        "disagreements": [{"link": list(i), "a": a[i], "b": b[i]} for i in items if a[i] != b[i]],
        "human_raters": humans,
        "note": "rater_a and rater_b are LLM agents labelling blind to each other; human_raters "
                "labelled the same items without seeing the model labels.",
    }
    (SCALE / "audit-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "disagreements"}, indent=2))
    print(len(summary["disagreements"]), "disagreements")


if __name__ == "__main__":
    main()
