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


def patch_asymmetry(patches: dict) -> dict[tuple[str, str], tuple[int, int]]:
    """For each ordered pair (a, b): the groups common to both, and those in
    which a declares at least one patch and b declares none."""
    both, only = {}, {}
    for row in patches["rows"]:
        counts = {}
        for rid, n in row["patch_counts"].items():
            counts.setdefault(rid.split(":", 1)[0], []).append(n)
        if any(n is None for ns in counts.values() for n in ns):
            continue
        for a in counts:
            for b in counts:
                if a != b:
                    both[a, b] = both.get((a, b), 0) + 1
                    only[a, b] = only.get((a, b), 0) + (max(counts[a]) > 0 and max(counts[b]) == 0)
    return {pair: (both[pair], only[pair]) for pair in both}


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
    for evidence, macro in (
        ("no packaging tarball", "nPatchNoTarball"),
        ("1.0 diff.gz format; patches not separable", "nPatchInseparable"),
    ):
        macros[macro] = fmt(sum(
            row["count"] is None and row["evidence"] == evidence
            for row in patches["records"].values()
        ))
    o = patches["outcome"]
    complete = o["all_unpatched"] + o["all_patched"] + o["mixed"]
    macros["nPatchMixed"] = fmt(o["mixed"])
    asym = patch_asymmetry(patches)
    four = [r["state"] for r in patches["rows"] if len(r["families"]) == 4 and r["state"] != "incomplete"]
    macros["nPatchFour"] = fmt(len(four))
    macros["nPatchFourMixed"] = fmt(four.count("mixed"))
    macros["pctPatchFourMixed"] = pct(four.count("mixed"), len(four))
    macros["nPatchFourAll"] = fmt(four.count("all_patched"))
    macros["nGroupsDebArch"] = fmt(asym["deb", "pkgbuild"][0])
    macros["pctOnlyDebNotArch"] = pct(asym["deb", "pkgbuild"][1], asym["deb", "pkgbuild"][0])
    macros["pctOnlyArchNotDeb"] = pct(asym["pkgbuild", "deb"][1], asym["pkgbuild", "deb"][0])
    for fam, tag in (("deb", "Deb"), ("rpm", "Rpm"), ("pkgbuild", "Arch"), ("ebuild", "Gentoo")):
        f = patches["per_family"][fam]
        macros[f"nPatchKnown{tag}"] = fmt(f["known"])
        macros[f"pctPatched{tag}"] = pct(f["patched"], f["known"])
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
    arch_summary = json.loads((SCALE / "pkgbuild-summary.json").read_text())
    arch_vcs_primary = arch_summary["artifacts_by_kind_role"]["vcs/primary"]
    macros["nArchVcsPrimary"] = fmt(arch_vcs_primary)
    macros["pctArchVcsPrimary"] = pct(arch_vcs_primary, census["records"]["pkgbuild"])
    macros["nEnrichedArch"] = fmt(arch["hashed"] - arch["declared_sha256_mismatch"])
    macros["nArchDigestMismatch"] = fmt(arch["declared_sha256_mismatch"])
    audit = json.loads((SCALE / "audit-summary.json").read_text())
    lo, hi = audit["same_primary_archive_wilson95_unweighted"]
    macros["nAuditItems"] = fmt(audit["items"])
    macros["nAuditSame"] = fmt(audit["both_same_primary_archive"])
    macros["nAuditAgree"] = fmt(audit["agreement"])
    macros["nAuditWrong"] = fmt(audit["both_wrong_product_or_vendored"])
    macros["auditCI"] = f"{100 * lo:.0f}--{100 * hi:.0f}\\%"
    macros["auditKappa"] = f"{audit['cohen_kappa']:.2f}"
    rep = json.loads((SCALE / "repology-compare.json").read_text())
    x = rep["audit_by_outcome"]
    macros["nRepSame"] = fmt(rep["outcomes"]["same_project"])
    macros["nRepDiff"] = fmt(rep["outcomes"]["different_project"])
    macros["pctRepSameWeighted"] = f"{round(rep['population_weighted']['same_project'] * 100)}\\%"
    macros["nRepSameSame"] = fmt(x["same_primary_archive"].get("same_project", 0))
    macros["nRepSameDiff"] = fmt(x["same_primary_archive"].get("different_project", 0))
    macros["nRepPartSame"] = fmt(x["component_or_split"].get("same_project", 0))
    macros["nRepPartDiff"] = fmt(x["component_or_split"].get("different_project", 0))
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
    for fam, tag in (("deb", "Deb"), ("rpm", "Rpm"), ("pkgbuild", "Arch"), ("ebuild", "Gentoo")):
        macros[f"pctGrouped{tag}"] = pct(patches["per_family"][fam]["records"], census["records"][fam])
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
            "\\caption{The snapshot. Records are source-package revisions. A file primary is an "
            "upstream archive with a declared digest (VCS sources excluded). Grouped records "
            "share a byte-identical primary archive with another distribution.}\n"
            "\\label{tab:frame}\n\\setlength{\\tabcolsep}{3pt}\n"
            "\\begin{tabularx}{\\columnwidth}{@{}lXrrr@{}}\n\\toprule\n"
            "Distribution & Source & Records & File primary & Grouped \\\\\n\\midrule\n"
            + "\n".join(rows) + "\n\\bottomrule\n\\end{tabularx}\n\\end{table}\n")
    (PROJECT / "tables" / "scale-frame.tex").write_text(body)

    names = json.loads((SCALE / "names.json").read_text())["pairs_in_groups_by_name_relation"]
    short = {"deb": "Deb", "rpm": "Fed", "pkgbuild": "Arch", "ebuild": "Gen"}
    rows = []
    # Same row order as figures/names-bars.pdf: rising share of same-named pairs.
    for pair in sorted(census["name_version_census"],
                       key=lambda p: names[p]["same_name"] / sum(names[p].values())):
        a, b = pair.split("-")
        c = census["name_version_census"][pair]
        same = c.get("linked_same_bytes", 0)
        diff = c.get("different_bytes", 0) + c.get("different_bytes_by_size", 0)
        rows.append(f"{labels[a]}--{labels[b]} & {fmt(same + diff)} & {pct(diff, same + diff)} \\\\")
    body = ("% Generated by scripts/scale/latex_numbers.py.\n\\begin{table}[!t]\n\\centering\n\\footnotesize\n"
            "\\caption{Matched by name and version: do the bytes agree? Pairs with the same name and "
            "normalized upstream version whose primary archives can be compared, and the share "
            "built from different bytes.}\n"
            "\\label{tab:names}\n"
            "\\begin{tabularx}{\\columnwidth}{@{}Xrr@{}}\n\\toprule\n"
            "Pair & Same name and version & Different bytes \\\\\n\\midrule\n"
            + "\n".join(rows) + "\n\\bottomrule\n\\end{tabularx}\n\\end{table}\n")
    (PROJECT / "tables" / "scale-names.tex").write_text(body)


if __name__ == "__main__":
    main()
