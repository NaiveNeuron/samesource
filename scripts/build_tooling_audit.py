#!/usr/bin/env python3
"""Blind manual audit of the build-tooling classifier.

Stage 1 (`sample`): draw 6 programs per recipe family with a seed derived
from the SHA-256 of evidence/provenance.json and write the sample to
evidence/build-tooling-audit.json. Blind labels are then added by hand
(without consulting evidence/build-tooling.json or the classifier rules).
Stage 2 (`compare`): load the classifier output and record agreement.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DATASET = Path.home() / ".cache/pkgbridge/rebuilt-record/dataset"
PROVENANCE = PROJECT / "evidence/provenance.json"
AUDIT = PROJECT / "evidence/build-tooling-audit.json"
CLASSIFIER = PROJECT / "evidence/build-tooling.json"
CLASSIFIER_FAMILY = {"nixpkgs": "nix"}
FAMILIES = ("deb", "rpm", "pkgbuild", "ebuild", "nixpkgs")
PER_FAMILY = 6
LABELS = ("cargo", "go", "python", "meson", "cmake", "autotools", "make", "implicit")


def sample() -> None:
    seed_hex = hashlib.sha256(PROVENANCE.read_bytes()).hexdigest()
    rng = random.Random(int(seed_hex, 16))
    entries = []
    for family in FAMILIES:
        programs = sorted(p.name for p in (DATASET / family).iterdir() if p.is_dir())
        for program in sorted(rng.sample(programs, PER_FAMILY)):
            files = sorted(f.name for f in (DATASET / family / program).iterdir() if f.is_file())
            entries.append({"family": family, "program": program, "files": files})
    AUDIT.write_text(json.dumps({
        "schema": "pkgbridge-build-tooling-audit/1",
        "seed": {
            "source": "sha256 of evidence/provenance.json bytes",
            "sha256": seed_hex,
            "rng": "Python random.Random(int(sha256, 16)); random.sample over sorted program "
                   "directory names, family order " + ", ".join(FAMILIES),
        },
        "per_family": PER_FAMILY,
        "label_set": list(LABELS),
        "sample": entries,
    }, indent=2) + "\n")


def compare() -> None:
    audit = json.loads(AUDIT.read_text())
    rows = {r["name"]: r["classes"] for r in json.loads(CLASSIFIER.read_text())["rows"]}
    per_family: dict[str, dict[str, int]] = {}
    disagreements = []
    for e in audit["sample"]:
        predicted = rows[e["program"]][CLASSIFIER_FAMILY.get(e["family"], e["family"])]
        e["classifier_label"] = predicted
        tally = per_family.setdefault(e["family"], {"agree": 0, "n": 0})
        tally["n"] += 1
        if predicted == e["blind_label"]:
            tally["agree"] += 1
        else:
            disagreements.append({
                "family": e["family"],
                "program": e["program"],
                "blind_label": e["blind_label"],
                "classifier_label": predicted,
                "justification": e["justification"],
            })
    agree = sum(t["agree"] for t in per_family.values())
    n = sum(t["n"] for t in per_family.values())
    audit["comparison"] = {
        "classifier_source": "evidence/build-tooling.json rows[].classes (family key 'nix' for nixpkgs)",
        "per_family": per_family,
        "overall": {"agree": agree, "n": n, "rate": round(agree / n, 4)},
        "disagreements": disagreements,
    }
    AUDIT.write_text(json.dumps(audit, indent=2) + "\n")


if __name__ == "__main__":
    {"sample": sample, "compare": compare}[sys.argv[1]]()
