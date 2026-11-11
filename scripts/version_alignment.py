#!/usr/bin/env python3
"""Corpus-wide check: do the five views of each identity share an upstream version?

Reads a corpus rebuilt by fetch_corpus.py plus evidence/provenance.json. Versions
are read literally from each recipe (no recipe code is evaluated); values that
need macro or Nix evaluation are reported as unresolved, never guessed. Upstream
publication dates come from the recovered commits and, for Debian, from
snapshot.debian.org first-seen times.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import time
import urllib.error
import re
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
FAMILIES = ["deb", "rpm", "pkgbuild", "ebuild", "nix"]
DIRS = {"deb": "deb", "rpm": "rpm", "pkgbuild": "pkgbuild", "ebuild": "ebuild", "nix": "nixpkgs"}


def literal(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.M)
    if not match:
        return None
    value = match.group(1).strip().strip("'\"")
    return None if re.search(r"[$%{}]", value) else value


def upstream_version(family: str, folder: Path) -> str | None:
    if family == "deb":
        raw = literal(r"^Version:\s*(\S+)", next(folder.glob("*.dsc")).read_text())
        if raw is None:
            return None
        raw = raw.split(":", 1)[-1]
        raw = raw.rsplit("-", 1)[0] if "-" in raw else raw
        return re.sub(r"[+~](dfsg|ds|repack)[^+~]*$", "", raw)
    if family == "rpm":
        return literal(r"^Version:\s*(\S+)", next(folder.glob("*.spec")).read_text())
    if family == "pkgbuild":
        raw = literal(r"^pkgver=(\S+)", (folder / "PKGBUILD").read_text())
        return raw.split(":", 1)[-1] if raw else None
    if family == "ebuild":
        match = re.search(r"-(\d[^-]*)(?:-r\d+)?\.ebuild$", next(folder.glob("*.ebuild")).name)
        return match.group(1) if match else None
    return literal(r'^\s*version\s*=\s*("[^"]*")\s*;', (folder / "default.nix").read_text())


def normalise(version: str) -> str:
    return re.sub(r"[._\-]+", ".", version.lower().lstrip("v"))


def debian_first_seen(sha1: str) -> str | None:
    url = f"https://snapshot.debian.org/mr/file/{sha1}/info"
    for attempt in range(4):  # snapshot.debian.org returns sporadic 5xx errors
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                rows = json.loads(response.read())["result"]
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
    seen = sorted(r["first_seen"] for r in rows if r["archive_name"] == "debian")
    if not seen:
        return None
    return datetime.strptime(seen[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="output root of fetch_corpus.py")
    parser.add_argument("--manifest", type=Path, default=PROJECT / "evidence" / "provenance.json")
    parser.add_argument("--output", type=Path, default=PROJECT / "evidence" / "version-alignment.json")
    parser.add_argument("--assignments", type=Path,
                        default=PROJECT / "evidence" / "public-inventory.json",
                        help="audit record holding the configured migration assignments")
    parser.add_argument("--identity-adjudication", type=Path,
                        default=PROJECT / "evidence" / "identity-adjudication.json",
                        help="adjudicated wrong joins, for the sensitivity summary")
    parser.add_argument("--debian-dates-from", type=Path,
                        help="reuse snapshot first-seen dates from an earlier output of this "
                             "script instead of querying snapshot.debian.org")
    args = parser.parse_args()

    provenance = {(r["family"], r["name"]): r
                  for r in json.loads(args.manifest.read_text())["records"]}
    names = sorted({name for _, name in provenance})
    deb_sha1 = {name: provenance[("deb", name)]["dsc_sha1"] for name in names}
    if args.debian_dates_from:
        earlier = json.loads(args.debian_dates_from.read_text())["rows"]
        deb_dates = {r["name"]: r["published"]["deb"] for r in earlier}
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            deb_dates = dict(zip(names, pool.map(debian_first_seen, [deb_sha1[n] for n in names])))

    rows = []
    for name in names:
        versions, dates, literals, reasons = {}, {}, {}, {}
        for family in FAMILIES:
            folder = args.corpus / "dataset" / DIRS[family] / name
            literal_version = upstream_version(family, folder)
            literals[family] = literal_version
            if literal_version is None:
                reasons[family] = "needs_evaluation"
            elif family == "ebuild" and re.search(r"(^|\.)9999$", literal_version):
                # A live ebuild builds VCS HEAD; 9999 is a marker, not a release.
                reasons[family] = "live_vcs"
                literal_version = None
            versions[family] = literal_version
            dates[family] = deb_dates[name] if family == "deb" else provenance[(family, name)]["date"]
        resolved = {f: normalise(v) for f, v in versions.items() if v}
        stamps = [datetime.fromisoformat(d) for d in dates.values() if d]
        rows.append({
            "name": name, "versions": versions, "literal_versions": literals,
            "unresolved_reasons": reasons, "published": dates,
            "resolved_families": len(resolved),
            "distinct_versions": len(set(resolved.values())),
            "all_agree": len(resolved) == 5 and len(set(resolved.values())) == 1,
            "publication_span_days": (max(stamps) - min(stamps)).days if stamps else None,
        })

    pairs = {}
    for a, b in itertools.combinations(FAMILIES, 2):
        both = [r for r in rows if r["versions"][a] and r["versions"][b]]
        same = sum(normalise(r["versions"][a]) == normalise(r["versions"][b]) for r in both)
        pairs[f"{a}-{b}"] = {"compared": len(both), "agree": same}
    full = [r for r in rows if r["resolved_families"] == 5]
    summary = {
        "identities": len(rows),
        "all_five_resolved": len(full),
        "all_five_agree": sum(r["all_agree"] for r in full),
        "unresolved_by_family": {f: sum(1 for r in rows if not r["versions"][f]) for f in FAMILIES},
        "live_vcs_by_family": {f: sum(1 for r in rows if r["unresolved_reasons"].get(f) == "live_vcs")
                               for f in FAMILIES},
        "disagreeing_with_majority_by_family": {},
        "pairwise": pairs,
        "median_publication_span_days": sorted(r["publication_span_days"] for r in rows)[len(rows) // 2],
    }
    for family in FAMILIES:
        count = 0
        for r in full:
            values = [normalise(v) for v in r["versions"].values()]
            majority = max(set(values), key=values.count)
            if values.count(majority) >= 3 and normalise(r["versions"][family]) != majority:
                count += 1
        summary["disagreeing_with_majority_by_family"][family] = count
    by_name = {r["name"]: r for r in rows}
    tasks = json.loads(args.assignments.read_text())["benchmark_assignments"]["assignments"]
    task_counts = {"same": 0, "different": 0, "unresolved": 0,
                   "different_involving_deb": 0}
    for task in tasks:
        versions = by_name[task["package"]]["versions"]
        source, target = versions[task["source"]], versions[task["target"]]
        if not source or not target:
            task_counts["unresolved"] += 1
        elif normalise(source) == normalise(target):
            task_counts["same"] += 1
        else:
            task_counts["different"] += 1
            task_counts["different_involving_deb"] += "deb" in (task["source"], task["target"])
    summary["configured_tasks"] = task_counts
    if args.identity_adjudication and args.identity_adjudication.exists():
        rejected = {(j["program"], j["family"]) for j in
                    json.loads(args.identity_adjudication.read_text())["rejected_joins"]}
        affected = {p for p, _ in rejected}
        kept = [r for r in full if r["name"] not in affected]
        summary["identity"] = {
            "rejected_views": len(rejected),
            "affected_programs": sorted(affected),
            "configured_tasks_using_rejected_view": sum(
                (t["package"], t["source"]) in rejected or (t["package"], t["target"]) in rejected
                for t in tasks),
            "all_five_resolved_excluding_affected": len(kept),
            "all_five_agree_excluding_affected": sum(r["all_agree"] for r in kept),
        }
        valid = {"same": 0, "different": 0, "unresolved": 0}
        for task in tasks:
            if ((task["package"], task["source"]) in rejected
                    or (task["package"], task["target"]) in rejected):
                continue
            versions = by_name[task["package"]]["versions"]
            source, target = versions[task["source"]], versions[task["target"]]
            key = ("unresolved" if not source or not target else
                   "same" if normalise(source) == normalise(target) else "different")
            valid[key] += 1
        summary["identity"]["configured_tasks_without_rejected_view"] = valid
    args.output.write_text(json.dumps({
        "schema": "pkgbridge-version-alignment/1",
        "interpretation": "Literal recipe versions after light normalisation; unresolved means "
                          "the value needs macro or language evaluation. Agreement is textual.",
        "summary": summary, "rows": rows}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
