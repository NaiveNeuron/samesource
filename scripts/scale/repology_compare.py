#!/usr/bin/env python3
"""Compare the audited SameSource links with Repology's project grouping.

For each side of the 180 sampled links (evidence/scale/audit-sheet.json) the
script asks Repology which project the source package belongs to in the
matching repository, and records whether both sides fall in one project.
Repology groups by curated names; SameSource by archive bytes, so agreement
is an external check of the links and disagreement shows what bytes add.

Repology is queried live (one request per second, cached), so its answer is
from the query date, not from the frame date; both dates are recorded.

Output: evidence/scale/repology-compare.json
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
CACHE = Path.home() / ".cache" / "pkgbridge" / "scale" / "repology"
REPO = {"deb": "debian_unstable", "rpm": "fedora_rawhide", "pkgbuild": "arch", "ebuild": "gentoo"}
AGENT = "samesource-research/1.0 (https://github.com/NaiveNeuron/samesource)"


def recorded() -> dict[tuple[str, str], str | None]:
    """Projects already stored in repology-compare.json, so the released
    comparison can be recomputed without querying Repology again."""
    path = SCALE / "repology-compare.json"
    if not path.exists():
        return {}
    out = {}
    for row in json.loads(path.read_text()).get("observations", []):
        for artifact, project in zip(row["link"], row["projects"]):
            family, rest = artifact.split(":", 1)
            out[family, rest.rsplit("@", 1)[0]] = project
    return out


RECORDED = recorded()


def project_of(family: str, name: str) -> str | None:
    """Repology project of a source package, or None when Repology has none."""
    if (family, name) in RECORDED:
        return RECORDED[family, name]
    cache = CACHE / f"{family}--{name.replace('/', '__')}.json"
    if cache.exists():
        return json.loads(cache.read_text())["project"]
    query = urllib.parse.urlencode({"repo": REPO[family], "name_type": "srcname", "name": name,
                                    "target_page": "api_v1_project"})
    request = urllib.request.Request(f"https://repology.org/tools/project-by?{query}",
                                     headers={"User-Agent": AGENT})
    for attempt in range(6):
        time.sleep(1.1 + 10 * attempt)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                final = response.geturl()
            break
        except urllib.error.HTTPError as error:
            if error.code == 404:
                final = ""
                break
            if attempt == 5:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 5:
                raise
    # A redirect to /api/v1/project/<name> is a unique match; anything else
    # (not found, or a disambiguation page) is recorded as no single project.
    marker = "/api/v1/project/"
    project = urllib.parse.unquote(final.split(marker, 1)[1]) if marker in final else None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"project": project, "url": final,
                                 "queried": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
    return project


def main() -> None:
    sheet = json.loads((SCALE / "audit-sheet.json").read_text())
    frame = json.loads((SCALE / "frame.json").read_text())
    rows = []
    for item in sheet["items"]:
        a, b = item["sides"]
        pa, pb = project_of(a["family"], a["source_package"]), project_of(b["family"], b["source_package"])
        outcome = "unresolved" if pa is None or pb is None else "same_project" if pa == pb else "different_project"
        rows.append({"pair": item["pair"], "link": item["link"], "projects": [pa, pb], "outcome": outcome})

    # Cross-tabulate with the audit label both LLM raters gave (they agree on every item).
    rater = json.loads((SCALE / "audit-rater-a.json").read_text())
    labels = rater["items"]
    verdict = {tuple(x["link"]): x["verdict"] for x in labels}
    cross: dict[str, dict[str, int]] = {}
    for r in rows:
        r["audit"] = verdict[tuple(r["link"])]
        cross.setdefault(r["audit"], {}).setdefault(r["outcome"], 0)
        cross[r["audit"]][r["outcome"]] += 1

    previous = SCALE / "repology-compare.json"
    # A replay from recorded observations keeps the date of the original queries.
    queried = (json.loads(previous.read_text())["repology_queried"] if RECORDED
               else datetime.now(timezone.utc).date().isoformat())
    population = sheet["population_by_pair"]
    total = sum(population.values())
    per_pair, weighted, variance = {}, {}, 0.0
    for outcome in ("same_project", "different_project", "unresolved"):
        weighted[outcome] = 0.0
    for pair, pop in population.items():
        its = [r for r in rows if r["pair"] == pair]
        counts = {o: sum(r["outcome"] == o for r in its) for o in weighted}
        per_pair[pair] = {"sampled": len(its), "population": pop, **counts}
        for o in weighted:
            weighted[o] += pop / total * counts[o] / len(its)
        p = counts["same_project"] / len(its)
        variance += (pop / total) ** 2 * p * (1 - p) / len(its)
    se = math.sqrt(variance)
    summary = {
        "schema": "pkgbridge-scale-repology-compare/1",
        "frame_date": frame.get("date") or frame.get("snapshot_date"),
        "repology_queried": queried,
        "repositories": REPO,
        "items": len(rows),
        "outcomes": {o: sum(r["outcome"] == o for r in rows) for o in weighted},
        "population_weighted": {o: round(v, 4) for o, v in weighted.items()},
        "same_project_weighted_ci95": [round(max(0.0, weighted["same_project"] - 1.96 * se), 4),
                                       round(min(1.0, weighted["same_project"] + 1.96 * se), 4)],
        "audit_by_outcome": cross,
        "per_pair": per_pair,
        "not_same_project": [r for r in rows if r["outcome"] != "same_project"],
        "observations": rows,
        "note": "Repology is queried live, so differences can also reflect changes after the frame date.",
    }
    (SCALE / "repology-compare.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ("outcomes", "population_weighted", "same_project_weighted_ci95")}))


if __name__ == "__main__":
    main()
