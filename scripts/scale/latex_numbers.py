#!/usr/bin/env python3
"""Emit tables/scale-numbers.tex: every scaled-dataset number the paper cites.

The manuscript uses only these macros, so regenerating evidence updates the
text. Each macro is derived from a file under evidence/scale/.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
FAMILIES = {"deb": "Deb", "rpm": "Rpm", "pkgbuild": "Arch", "ebuild": "Gentoo"}


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", "{,}")


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}\\%" if b else "--"


def main() -> None:
    macros = {}
    census = json.loads((SCALE / "census.json").read_text())
    for fam, label in FAMILIES.items():
        macros[f"nRecords{label}"] = fmt(census["records"].get(fam, 0))
    macros["nRecordsAll"] = fmt(sum(census["records"].values()))
    macros["nGroups"] = fmt(census["groups_spanning_families"])
    by_count = census["groups_by_family_count"]
    for k, word in (("2", "Two"), ("3", "Three"), ("4", "Four")):
        macros[f"nGroups{word}"] = fmt(by_count.get(k, 0))
    macros["nGroupsThreePlus"] = fmt(sum(v for k, v in by_count.items() if int(k) >= 3))
    nv = census["name_version_census"]
    same = sum(v.get("linked_same_bytes", 0) for v in nv.values())
    diff = sum(v.get("different_bytes", 0) + v.get("different_bytes_by_size", 0) for v in nv.values())
    macros["nNameSame"] = fmt(same)
    macros["nNameDiff"] = fmt(diff)
    macros["pctNameDiff"] = pct(diff, same + diff)
    names = json.loads((SCALE / "names.json").read_text())["pairs_in_groups_by_name_relation"]
    cross = sum(v.get("different_name", 0) for v in names.values())
    prefix = sum(v.get("same_after_ecosystem_prefix", 0) for v in names.values())
    total_pairs = sum(sum(v.values()) for v in names.values())
    macros["nCrossName"] = fmt(cross)
    macros["nPrefixName"] = fmt(prefix)
    macros["nGroupPairs"] = fmt(total_pairs)
    macros["pctNotSameName"] = pct(cross + prefix, total_pairs)
    patches = json.loads((SCALE / "patches.json").read_text())
    o = patches["outcome"]
    complete = o["all_unpatched"] + o["all_patched"] + o["mixed"]
    macros["nPatchMixed"] = fmt(o["mixed"])
    macros["nPatchComplete"] = fmt(complete)
    macros["pctPatchMixed"] = pct(o["mixed"], complete)
    macros["nPatchAll"] = fmt(o["all_patched"])
    tasks = json.loads((SCALE / "tasks-summary.json").read_text())
    macros["nTasks"] = fmt(tasks["tasks"])
    macros["nTaskDirections"] = fmt(len(tasks["by_direction"]))
    enrich = json.loads((SCALE / "deb-enrichment-summary.json").read_text())
    macros["nEnriched"] = fmt(enrich["hashed"])
    macros["gbEnriched"] = f"{enrich['bytes_streamed'] / 1e9:.0f}"
    arch = json.loads((SCALE / "pkgbuild-enrichment-summary.json").read_text())
    macros["nEnrichedArch"] = fmt(arch["hashed"])
    macros["nArchDigestMismatch"] = fmt(arch["declared_sha256_mismatch"])
    audit = json.loads((SCALE / "audit-summary.json").read_text())
    lo, hi = audit["same_primary_archive_wilson95_unweighted"]
    macros["nAuditItems"] = fmt(audit["items"])
    macros["nAuditSame"] = fmt(audit["both_same_primary_archive"])
    macros["nAuditAgree"] = fmt(audit["agreement"])
    macros["nAuditWrong"] = fmt(audit["both_wrong_product_or_vendored"])
    macros["auditCI"] = f"{100 * lo:.0f}--{100 * hi:.0f}\\%"
    macros["auditKappa"] = f"{audit['cohen_kappa']:.2f}"
    humans = audit.get("human_raters") or {}
    if len(humans) == 1:
        h = next(iter(humans.values()))
        macros["nHumanSame"] = fmt(h["verdicts"].get("same_primary_archive", 0))
        macros["nHumanPart"] = fmt(h["verdicts"].get("component_or_split", 0))
        macros["nHumanAgree"] = fmt(h["agrees_with_both_models"])
        macros["nHumanDisagree"] = fmt(len(h["disagreements"]))
        macros["nHumanWrong"] = fmt(h["wrong_product_or_vendored"] + h["unclear"])
        macros["humanKappa"] = f"{h['cohen_kappa_vs_models']:.2f}"
        if "after_discussion" in h:
            macros["nHumanRevised"] = fmt(h["after_discussion"]["revised"])
            macros["nHumanAgreeAfter"] = fmt(h["after_discussion"]["agrees_with_both_models"])
            macros["nHumanDisagreeAfter"] = fmt(len(h["after_discussion"]["remaining_disagreements"]))
    w, se = audit["same_primary_archive_population_weighted"], audit["same_primary_archive_weighted_se"]
    macros["auditWeighted"] = f"{100 * w:.1f}\\%"
    macros["auditWeightedCI"] = f"{100 * max(0, w - 1.96 * se):.0f}--{100 * min(1, w + 1.96 * se):.0f}\\%"
    macros["nArchFetchErrors"] = fmt(arch["errors"])
    causes = json.loads((SCALE / "mismatch-causes.json").read_text())
    total = causes["total"]
    for key, name in (("compression_or_rename_only", "Recompress"), ("forge_archive", "Forge"),
                      ("debian_repack", "Repack"), ("crate_vs_archive", "Crate"), ("other", "Other")):
        macros[f"pctCause{name}"] = pct(causes["by_cause"].get(key, 0), total)
    grouped = sum(1 for line in (SCALE / "groups.jsonl").open() for _ in json.loads(line)["records"])
    macros["nGroupedRecords"] = fmt(grouped)
    macros["pctGroupedRecords"] = pct(grouped, sum(census["records"].values()))
    lines = ["% Generated by scripts/scale/latex_numbers.py from evidence/scale/; do not edit."]
    lines += [f"\\newcommand{{\\{k}}}{{{v}}}" for k, v in sorted(macros.items())]
    out = PROJECT / "tables" / "scale-numbers.tex"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    write_tables(census)


def write_tables(census: dict) -> None:
    labels = {"deb": "Debian", "rpm": "Fedora", "pkgbuild": "Arch", "ebuild": "Gentoo"}
    snapshots = {"deb": "snapshot 2026-11-05", "rpm": "rawhide 20261105.n.0",
                 "pkgbuild": "archive 2026-11-05", "ebuild": "mirror ebeaca6e"}
    grouped = {}
    for line in (SCALE / "groups.jsonl").open():
        for rid in json.loads(line)["records"]:
            grouped[rid.split(":", 1)[0]] = grouped.get(rid.split(":", 1)[0], 0) + 1
    rows = []
    for fam, label in labels.items():
        recs = with_file_primary = 0
        for line in (SCALE / f"{fam}.jsonl").open():
            r = json.loads(line)
            recs += 1
            with_file_primary += any(a["role"] == "primary" and a["hash_domain"] == "file_bytes"
                                     for a in r["artifacts"])
        rows.append(f"{label} & {snapshots[fam]} & {fmt(recs)} & {fmt(with_file_primary)} & "
                    f"{fmt(grouped.get(fam, 0))} \\\\")
    body = ("% Generated by scripts/scale/latex_numbers.py.\n\\begin{table}[!t]\n\\centering\n\\footnotesize\n"
            "\\caption{Frozen frame. Records are source-package revisions; a file primary is an "
            "upstream archive with a declared digest (VCS sources excluded); grouped records "
            "share a byte-identical primary archive with another distribution.}\n"
            "\\label{tab:frame}\n\\setlength{\\tabcolsep}{3pt}\n"
            "\\begin{tabularx}{\\columnwidth}{@{}lXrrr@{}}\n\\toprule\n"
            "Distribution & Frame & Records & File primary & Grouped \\\\\n\\midrule\n"
            + "\n".join(rows) + "\n\\bottomrule\n\\end{tabularx}\n\\end{table}\n")
    (PROJECT / "tables" / "scale-frame.tex").write_text(body)

    names = json.loads((SCALE / "names.json").read_text())["pairs_in_groups_by_name_relation"]
    short = {"deb": "Deb", "rpm": "Fed", "pkgbuild": "Arch", "ebuild": "Gen"}
    rows = []
    for pair in sorted(census["name_version_census"]):
        a, b = pair.split("-")
        n = names.get(pair, {})
        linked = sum(n.values())
        renamed = n.get("different_name", 0) + n.get("same_after_ecosystem_prefix", 0)
        c = census["name_version_census"][pair]
        same = c.get("linked_same_bytes", 0)
        diff = c.get("different_bytes", 0) + c.get("different_bytes_by_size", 0)
        rows.append(f"{short[a]}--{short[b]} & {fmt(linked)} & {pct(renamed, linked)} & "
                    f"{fmt(same + diff)} & {pct(diff, same + diff)} \\\\")
    body = ("% Generated by scripts/scale/latex_numbers.py.\n\\begin{table}[!t]\n\\centering\n\\footnotesize\n"
            "\\caption{Names against bytes, per distribution pair. Left: package pairs that share "
            "a primary archive, and the share whose names differ (including ecosystem prefixes). "
            "Right: pairs with the same name and upstream version whose primary archives can be "
            "compared, and the share built from different bytes.}\n"
            "\\label{tab:names}\n"
            "\\begin{tabularx}{\\columnwidth}{@{}Xrrrr@{}}\n\\toprule\n"
            "Pair & Same bytes & Renamed & Same name+ver. & Diff.\\ bytes \\\\\n\\midrule\n"
            + "\n".join(rows) + "\n\\bottomrule\n\\end{tabularx}\n\\end{table}\n")
    (PROJECT / "tables" / "scale-names.tex").write_text(body)


if __name__ == "__main__":
    main()
