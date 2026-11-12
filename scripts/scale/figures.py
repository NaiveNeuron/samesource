#!/usr/bin/env python3
"""Draw the paper's data figure from evidence/scale/names.json.

Output: figures/names-bars.pdf (needs matplotlib).
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
NAMES = {"deb": "Debian", "rpm": "Fedora", "pkgbuild": "Arch", "ebuild": "Gentoo"}
# Light, mid and dark, so the three parts stay apart in greyscale print.
PARTS = (("same_name", "#cfd8d4", "black", "same name"),
         ("same_after_ecosystem_prefix", "#e0a13a", "black", "differs by a prefix"),
         ("different_name", "#9c1f1a", "white", "different name"))


def main() -> None:
    pairs = json.loads((PROJECT / "evidence" / "scale" / "names.json").read_text())["pairs_in_groups_by_name_relation"]
    order = sorted(pairs, key=lambda p: pairs[p]["same_name"] / sum(pairs[p].values()))
    plt.rcParams.update({"font.family": "serif", "font.size": 8, "axes.spines.top": False,
                         "axes.spines.right": False, "pdf.fonttype": 42})
    fig, ax = plt.subplots(figsize=(3.45, 1.75))
    for row, pair in enumerate(order):
        total, left = sum(pairs[pair].values()), 0.0
        for key, colour, ink, label in PARTS:
            share = pairs[pair][key] / total * 100
            ax.barh(row, share, left=left, color=colour, height=0.7, label=label if row == 0 else None)
            if share >= 6:
                ax.text(left + share / 2, row, f"{round(share)}%", ha="center", va="center", color=ink, fontsize=7)
            left += share
    ax.set_yticks(range(len(order)), ["–".join(NAMES[f] for f in p.split("-")) + f" ({sum(pairs[p].values()):,})"
                                      for p in order])
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of the pairs that build from the same archive (%)")
    ax.legend(ncol=3, loc="lower center", bbox_to_anchor=(0.36, 0.98), frameon=False, fontsize=7,
              handlelength=1, columnspacing=1)
    fig.savefig(PROJECT / "figures" / "names-bars.pdf", bbox_inches="tight", pad_inches=0.01)


if __name__ == "__main__":
    main()
