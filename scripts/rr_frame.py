#!/usr/bin/env python3
"""Pilot sampling frame for the registered study: same-version candidates.

Enumerates source packages in Debian unstable (Sources.xz), Fedora rawhide
(source primary.xml), and Arch Linux core/extra (repository .db files) and
counts candidates whose source-package names and upstream versions agree.
Name agreement is only candidate generation; the protocol confirms identity
from source archives afterwards. Inputs are hashed so the frame is pinned.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import lzma
from pathlib import Path
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]


def norm(version: str) -> str:
    return re.sub(r"[._\-+~]+", ".", version.lower().lstrip("v"))


def unique(records: list[tuple[str, str, str]]) -> dict[str, str]:
    """Map name -> upstream version, dropping names listed with several versions."""
    versions: dict[str, set[str]] = {}
    upstream: dict[str, str] = {}
    for name, full, up in records:
        versions.setdefault(name, set()).add(full)
        upstream[name] = up
    return {n: upstream[n] for n, v in versions.items() if len(v) == 1}


def debian(path: Path) -> dict[str, str]:
    records = []
    for stanza in lzma.open(path, "rt", encoding="utf-8").read().split("\n\n"):
        fields = dict(re.findall(r"^(Package|Version): (.+)$", stanza, re.M))
        if "Package" not in fields:
            continue
        upstream = fields["Version"].split(":", 1)[-1]
        upstream = upstream.rsplit("-", 1)[0] if "-" in upstream else upstream
        upstream = re.sub(r"[+~](dfsg|ds|repack)[^+~]*$", "", upstream)
        records.append((fields["Package"].lower(), fields["Version"], upstream))
    return unique(records)


def fedora(path: Path) -> dict[str, str]:
    xml = subprocess.run(["zstd", "-dc", str(path)], check=True, capture_output=True).stdout
    ns = {"c": "http://linux.duke.edu/metadata/common"}
    records = []
    for pkg in ET.parse(io.BytesIO(xml)).getroot().findall("c:package", ns):
        version = pkg.find("c:version", ns)
        records.append((pkg.findtext("c:name", namespaces=ns).lower(),
                        f"{version.get('ver')}-{version.get('rel')}", version.get("ver")))
    return unique(records)


def arch(paths: list[Path]) -> dict[str, str]:
    records = []
    for path in paths:
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                if not member.name.endswith("/desc"):
                    continue
                text = tar.extractfile(member).read().decode()
                fields = dict(re.findall(r"%(\w+)%\n([^\n]+)", text))
                version = fields["VERSION"].split(":", 1)[-1].rsplit("-", 1)[0]
                records.append((fields.get("BASE", fields["NAME"]).lower(),
                                fields["VERSION"], version))
    return unique(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True,
                        help="directory with Sources.xz, primary.xml.zst, core.db, extra.db")
    parser.add_argument("--output", type=Path, default=PROJECT / "evidence" / "rr-frame-pilot.json")
    args = parser.parse_args()
    m = args.metadata
    inputs = {"deb": [m / "Sources.xz"], "rpm": [m / "primary.xml.zst"],
              "pkgbuild": [m / "core.db", m / "extra.db"]}
    families = {"deb": debian(m / "Sources.xz"), "rpm": fedora(m / "primary.xml.zst"),
                "pkgbuild": arch([m / "core.db", m / "extra.db"])}
    pairs = {}
    for a, b in itertools.permutations(families, 2):
        if a > b:
            continue
        common = families[a].keys() & families[b].keys()
        same = sorted(n for n in common if norm(families[a][n]) == norm(families[b][n]))
        pairs[f"{a}-{b}"] = {"name_matches": len(common), "same_version": len(same)}
    all_three = sorted(n for n in families["deb"].keys() & families["rpm"].keys()
                       & families["pkgbuild"].keys()
                       if len({norm(families[f][n]) for f in families}) == 1)
    result = {
        "schema": "pkgbridge-rr-frame-pilot/1",
        "interpretation": "Candidates by exact source-package name and normalised upstream "
                          "version; identity is not yet confirmed from source archives.",
        "inputs_sha256": {f: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ps}
                          for f, ps in inputs.items()},
        "source_packages": {f: len(v) for f, v in families.items()},
        "pairs": pairs,
        "same_version_in_all_three": len(all_three),
        "all_three_names": all_three,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "all_three_names"}, indent=2))


if __name__ == "__main__":
    main()
