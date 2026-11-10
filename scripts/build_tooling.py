#!/usr/bin/env python3
"""Second corpus-wide use: do recipes for one program declare the same build tooling?

Reads the rebuilt corpus as text and assigns each recipe the first matching
class in a fixed order (cargo, go, python, meson, cmake, autotools, make) from
literal markers listed in RULES; recipes that rely on default phases without a
marker are "implicit". This is declared tooling, not an observed build: an
implicit Debian dh or Nix stdenv recipe may run any of these systems.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

PROJECT = Path(__file__).resolve().parents[1]
DIRS = {"deb": "deb", "rpm": "rpm", "pkgbuild": "pkgbuild", "ebuild": "ebuild", "nix": "nixpkgs"}
ORDER = ["cargo", "go", "python", "meson", "cmake", "autotools", "make"]
RULES = {
    # Debian: an explicit --buildsystem wins (see classify_deb); otherwise only
    # Build-Depends and rules markers below are consulted.
    "deb": {
        "cargo": [r"\bdh-cargo\b", r"\bcargo build\b"],
        "go": [r"\bdh-golang\b"],
        "python": [],
        "meson": [r"\bmeson\b"],
        "cmake": [r"\bcmake\b"],
        "autotools": [r"\bdh-autoreconf\b", r"--with[= ]autoreconf", r"\bautoconf\b",
                      r"\./configure\b"],
        "make": [r"\$\(MAKE\)", r"^\s*make\b"],
    },
    "rpm": {
        "cargo": [r"%cargo_build"], "go": [r"%gobuild", r"\bgo build\b"],
        "python": [r"%pyproject_wheel", r"%py3_build"], "meson": [r"%meson\b"],
        "cmake": [r"%cmake\b"], "autotools": [r"%configure\b", r"autoreconf"],
        "make": [r"%make_build", r"^\s*make\b"],
    },
    "pkgbuild": {
        "cargo": [r"\bcargo build\b"], "go": [r"\bgo build\b"],
        "python": [r"python -m (build|installer)", r"setup\.py"],
        "meson": [r"\b(arch-)?meson\b"], "cmake": [r"\bcmake\b"],
        "autotools": [r"\./configure\b", r"autoreconf", r"\bautogen\.sh\b"],
        "make": [r"^\s*make\b"],
    },
    "ebuild": {
        "cargo": [r"^inherit\b.*\bcargo\b"], "go": [r"^inherit\b.*\bgo-module\b"],
        "python": [r"^inherit\b.*\bdistutils-r1\b"], "meson": [r"^inherit\b.*\bmeson\b"],
        "cmake": [r"^inherit\b.*\bcmake\b"],
        "autotools": [r"^inherit\b.*\bautotools\b", r"\beconf\b"], "make": [r"\bemake\b"],
    },
    "nix": {
        "cargo": [r"\bbuildRustPackage\b"], "go": [r"\bbuildGoModule\b"],
        "python": [r"\bbuildPython(Application|Package)\b"],
        "meson": [r"nativeBuildInputs[^;]*\bmeson\b"],
        "cmake": [r"nativeBuildInputs[^;]*\bcmake\b"],
        "autotools": [r"\bautoreconfHook\b"], "make": [r"\bmakeFlags\b"],
    },
}


def recipe_text(family: str, folder: Path) -> str:
    if family == "deb":
        return (folder / "rules").read_text() + "\0" + (folder / "control").read_text()
    pattern = {"rpm": "*.spec", "pkgbuild": "PKGBUILD", "ebuild": "*.ebuild", "nix": "default.nix"}
    return next(folder.glob(pattern[family])).read_text()


DEB_BUILDSYSTEMS = {"cargo": "cargo", "golang": "go", "pybuild": "python", "meson": "meson",
                    "cmake": "cmake", "autoconf": "autotools", "makefile": "make"}


def classify_deb(rules: str, control: str) -> str:
    explicit = re.search(r"--buildsystem[= ]([a-z_]+)", rules)
    if explicit and explicit.group(1) in DEB_BUILDSYSTEMS:
        return DEB_BUILDSYSTEMS[explicit.group(1)]
    depends = re.search(r"^Build-Depends(?:-Arch|-Indep)?:(.*?)(?=^\S)", control, re.M | re.S)
    text = rules + "\n" + (depends.group(1) if depends else "")
    return classify_text("deb", text)


def classify(family: str, text: str) -> str:
    if family == "deb":
        rules, control = text.split("\0", 1)
        return classify_deb(rules, control)
    return classify_text(family, text)


def classify_text(family: str, text: str) -> str:
    for cls in ORDER:
        if any(re.search(p, text, re.M | re.S) for p in RULES[family][cls]):
            return cls
    return "implicit"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="output root of fetch_corpus.py")
    parser.add_argument("--output", type=Path, default=PROJECT / "evidence" / "build-tooling.json")
    args = parser.parse_args()
    root = args.corpus / "dataset"
    names = sorted(p.name for p in (root / "rpm").iterdir())
    rows = []
    for name in names:
        classes = {f: classify(f, recipe_text(f, root / d / name)) for f, d in DIRS.items()}
        declared = {f: c for f, c in classes.items() if c != "implicit"}
        rows.append({"name": name, "classes": classes,
                     "declared_families": len(declared),
                     "agree": len(set(declared.values())) == 1 if len(declared) >= 2 else None})
    comparable = [r for r in rows if r["agree"] is not None]
    summary = {
        "programs": len(rows),
        "class_counts_by_family": {f: dict(Counter(r["classes"][f] for r in rows)) for f in DIRS},
        "programs_with_two_or_more_declared": len(comparable),
        "declared_tooling_agrees": sum(r["agree"] for r in comparable),
        "disagreeing_examples": [{"name": r["name"], "classes": r["classes"]}
                                 for r in comparable if not r["agree"]],
    }
    args.output.write_text(json.dumps({
        "schema": "pkgbridge-build-tooling/1",
        "interpretation": "Declared build tooling from literal markers (RULES in the script); "
                          "'implicit' means no marker, not an unknown or missing build system.",
        "summary": summary, "rows": rows}, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "disagreeing_examples"}, indent=2))
    print(len(summary["disagreeing_examples"]), "disagreements")


if __name__ == "__main__":
    main()
