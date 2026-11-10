#!/usr/bin/env python3
"""Extract v1 records for Debian unstable source packages from the pinned frame.

Reads only CACHE/frame/debian-Sources.xz (snapshot recorded in
evidence/scale/frame.json) and writes one record per source stanza to
evidence/scale/deb.jsonl plus counts to evidence/scale/deb-summary.json.
Artifacts come from Checksums-Sha256 (name, size, sha256) with the MD5 from
Files; the .dsc is the packaging closure, not an artifact. Offline: no network.
"""

from __future__ import annotations

import collections
import hashlib
import json
import lzma
from pathlib import Path
import re
import subprocess

PROJECT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache" / "pkgbridge" / "scale"
FRAME = PROJECT / "evidence" / "scale" / "frame.json"
OUT = PROJECT / "evidence" / "scale" / "deb.jsonl"
SUMMARY = PROJECT / "evidence" / "scale" / "deb-summary.json"
MIRROR = "http://deb.debian.org/debian"

REPACK_SUFFIX = re.compile(r"[+~](dfsg|ds|repack)[^+~]*$")  # as in scripts/rr_frame.py
REPACK_MARK = re.compile(r"[+~.](dfsg|ds|repack)", re.I)
ORIG = re.compile(r"\.orig\.tar\.[A-Za-z0-9]+$")
COMPONENT = re.compile(r"\.orig-([A-Za-z0-9][A-Za-z0-9-]*)\.tar\.[A-Za-z0-9]+$")
DEBIAN_TAR = re.compile(r"\.debian\.tar\.[A-Za-z0-9]+$")
TARBALL = re.compile(r"\.tar\.[A-Za-z0-9]+$")


def parse_stanza(text: str) -> dict[str, str]:
    """deb822 stanza -> field map; continuation lines joined with newlines."""
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key is not None:
            fields[key] += "\n" + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def checksum_lines(value: str) -> list[tuple[str, int, str]]:
    rows = []
    for line in value.splitlines():
        parts = line.split()
        if len(parts) == 3:
            rows.append((parts[2], int(parts[1]), parts[0]))
    return rows


def upstream_of(version: str) -> tuple[str, str | None]:
    """Drop epoch, Debian revision, then a trailing +dfsg/+ds/+repack suffix."""
    up = version.split(":", 1)[-1]
    up = up.rsplit("-", 1)[0] if "-" in up else up
    stripped = REPACK_SUFFIX.sub("", up)
    return stripped, (up if stripped != up else None)


def classify(name: str, prefix: str, names: set[str], native: bool,
             repack: bool, fmt: str) -> tuple[str, str, str]:
    """Return (kind, role, role_evidence) for one file of a source stanza."""
    if name.endswith((".asc", ".sig", ".sign")):
        target = name.rsplit(".", 1)[0]
        present = "listed" if target in names else "not listed"
        return "signature", "signature", f"detached signature suffix; signs {target} ({present} in stanza)"
    if ORIG.search(name):
        ev = "Debian .orig.tar.* upstream tarball of the main source"
        if repack:
            ev += "; repacked by Debian (version carries +dfsg/+ds/+repack), bytes differ from upstream release"
        return "tarball", "primary", ev
    m = COMPONENT.search(name)
    if m:
        return "tarball", "component", f"Debian .orig-{m.group(1)}.tar.* additional upstream component"
    if DEBIAN_TAR.search(name):
        return "tarball", "packaging", "Debian .debian.tar.* packaging tarball"
    if name.endswith(".diff.gz"):
        return "patch", "packaging", "Debian format 1.0 .diff.gz packaging diff"
    if TARBALL.search(name):
        if native and name.startswith(prefix + "."):
            return ("tarball", "primary",
                    f"Debian native source tarball (Format {fmt}, no .orig); includes debian/ packaging")
        return "tarball", "unknown", "tarball not matching .orig/.orig-<c>/.debian/native naming"
    return "file", "unknown", "filename matches no Debian source-file pattern"


def record(fields: dict[str, str], snapshot: str, collector: str,
           failures: collections.Counter) -> dict:
    pkg, version = fields["Package"], fields["Version"]
    rid = f"deb:{pkg}@{version}"
    notes: list[str] = []
    fmt = fields.get("Format", "")
    directory = fields.get("Directory", "")
    noepoch = version.split(":", 1)[-1]
    upstream, pre_strip = upstream_of(version)
    if pre_strip is not None:
        notes.append(f"upstream_version stripped repack suffix from '{pre_strip}'")
    unstripped_mark = REPACK_MARK.search(upstream)
    if unstripped_mark:
        notes.append(f"upstream_version still carries repack marker '{unstripped_mark.group(0)}' "
                     "(not a trailing suffix)")
    repack = bool(REPACK_MARK.search(noepoch.rsplit("-", 1)[0] if "-" in noepoch else noepoch))

    sha = checksum_lines(fields.get("Checksums-Sha256", ""))
    md5 = {n: (s, h) for n, s, h in checksum_lines(fields.get("Files", ""))}
    names = {n for n, _, _ in sha}
    if not sha:
        failures["no_checksums_sha256"] += 1
        notes.append("stanza has no Checksums-Sha256 entries")
    for n in sorted(set(md5) - names):
        failures["file_only_in_Files"] += 1
        notes.append(f"{n} listed in Files but not in Checksums-Sha256; not recorded as artifact")

    dsc_name = f"{pkg}_{noepoch}.dsc"
    dsc = [(n, s, h) for n, s, h in sha if n.endswith(".dsc")]
    dsc_match = [row for row in dsc if row[0] == dsc_name] or dsc
    if len(dsc_match) != 1:
        failures["dsc_missing_or_ambiguous"] += 1
        notes.append(f"expected one .dsc ({dsc_name}); found {[r[0] for r in dsc]}")
        dsc_row = None
    else:
        dsc_row = dsc_match[0]
        if dsc_row[0] != dsc_name:
            notes.append(f".dsc name {dsc_row[0]} differs from expected {dsc_name}")

    has_orig = any(ORIG.search(n) for n in names)
    has_native_tar = any(TARBALL.search(n) and not ORIG.search(n) and not COMPONENT.search(n)
                         and not DEBIAN_TAR.search(n) for n in names)
    native = fmt == "3.0 (native)" or (not has_orig and has_native_tar
                                       and not any(n.endswith(".diff.gz") for n in names))
    if fmt == "3.0 (native)" and has_orig:
        notes.append("Format 3.0 (native) but an .orig tarball is listed")

    artifacts = []
    prefix = f"{pkg}_{noepoch}"
    for name, size, digest in sha:  # Checksums-Sha256 order: dsc, orig(s), debian
        if dsc_row is not None and name == dsc_row[0]:
            continue
        kind, role, evidence = classify(name, prefix, names, native, repack, fmt)
        declared = {"sha256": digest}
        if name in md5:
            if md5[name][0] != size:
                failures["size_mismatch_Files_vs_Sha256"] += 1
                notes.append(f"{name}: Files size {md5[name][0]} != Checksums-Sha256 size {size}")
            declared["md5"] = md5[name][1]
        else:
            failures["artifact_missing_md5"] += 1
            notes.append(f"{name}: not listed in Files (no md5)")
        if role == "unknown":
            failures["unclassified_artifact"] += 1
        artifacts.append({
            "id": f"{rid}#{len(artifacts)}", "name": name,
            "url": f"{MIRROR}/{directory}/{name}" if directory else None,
            "size": size, "kind": kind, "role": role, "role_evidence": evidence,
            "declared": declared, "hash_domain": "file_bytes", "condition": None,
        })
    if artifacts:
        notes.append("artifact urls use deb.debian.org (current mirror); the pinned copy is "
                     f"snapshot.debian.org/archive/debian/{snapshot}/{directory}/")
    if fields.get("Extra-Source-Only") == "yes":
        notes.append("Extra-Source-Only: yes (kept in the archive for Built-Using/licence "
                     "compliance, not the current unstable source)")
    if "Vendored-Sources-Rust" in fields:
        notes.append("stanza declares Vendored-Sources-Rust (vendored crates inside the source)")

    primaries = [a for a in artifacts if a["role"] == "primary"]
    if not directory:
        failures["no_directory"] += 1
        notes.append("stanza has no Directory field; urls and metadata_pointer unresolved")
    if not artifacts:
        resolution = "unresolved"
    elif primaries and dsc_row is not None and directory:
        resolution = "resolved"
    else:
        resolution = "partial"
        if not primaries:
            failures["no_primary_artifact"] += 1
            notes.append("no primary artifact identified")

    binaries = [b.strip() for b in fields.get("Binary", "").replace("\n", " ").split(",") if b.strip()]
    return {
        "id": rid, "family": "deb", "source_package": pkg, "version": version,
        "upstream_version": upstream, "binary_packages": binaries,
        "packaging": {
            "kind": "debian-source",
            "repository": f"https://snapshot.debian.org/archive/debian/{snapshot}/",
            "commit": None, "path": directory or None, "how_pinned": "snapshot-dsc",
            "metadata_pointer": (f"https://snapshot.debian.org/archive/debian/{snapshot}/"
                                 f"{directory}/{dsc_row[0]}") if dsc_row and directory else None,
            "metadata_sha256": dsc_row[2] if dsc_row else None,
        },
        "artifacts": artifacts, "live_vcs": False, "native": native,
        "resolution": resolution, "notes": notes, "collector": collector,
    }


def main() -> None:
    frame = json.loads(FRAME.read_text())
    source = frame["files"]["debian_sources"]
    path = CACHE / source["path"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != source["sha256"]:
        raise SystemExit(f"{path} sha256 {digest} != frame {source['sha256']}")
    snapshot = frame["debian_snapshot"]
    head = subprocess.check_output(["git", "-C", str(PROJECT), "rev-parse", "--short", "HEAD"],
                                   text=True).strip()
    collector = f"scripts/scale/extract_deb.py@{head}"

    failures: collections.Counter = collections.Counter()
    records = []
    stanzas = 0
    for text in lzma.open(path, "rt", encoding="utf-8").read().split("\n\n"):
        if not text.strip():
            continue
        stanzas += 1
        fields = parse_stanza(text)
        if "Package" not in fields or "Version" not in fields:
            failures["stanza_without_package_or_version"] += 1
            continue
        records.append(record(fields, snapshot, collector, failures))
    records.sort(key=lambda r: r["id"])
    ids = [r["id"] for r in records]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate record ids")

    with OUT.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    kinds, roles, algos, res = (collections.Counter() for _ in range(4))
    for r in records:
        res[r["resolution"]] += 1
        for a in r["artifacts"]:
            kinds[a["kind"]] += 1
            roles[a["role"]] += 1
            algos.update(a["declared"].keys())
    per_pkg = collections.Counter(r["source_package"] for r in records)
    summary = {
        "family": "deb", "collector": collector,
        "input": {"path": source["path"], "url": source["url"], "sha256": digest,
                  "snapshot": snapshot},
        "stanzas": stanzas, "records": len(records),
        "source_packages": len(per_pkg),
        "source_packages_with_multiple_versions": sum(1 for v in per_pkg.values() if v > 1),
        "native_records": sum(r["native"] for r in records),
        "live_vcs_records": sum(r["live_vcs"] for r in records),
        "records_with_primary": sum(any(a["role"] == "primary" for a in r["artifacts"])
                                    for r in records),
        "records_with_repack_primary": sum(any(a["role"] == "primary" and "repacked" in
                                               a["role_evidence"] for a in r["artifacts"])
                                           for r in records),
        "records_with_unstripped_repack_marker": sum(any("still carries repack marker" in n
                                                         for n in r["notes"]) for r in records),
        "artifacts": sum(kinds.values()),
        "artifacts_by_kind": dict(sorted(kinds.items())),
        "artifacts_by_role": dict(sorted(roles.items())),
        "hash_algorithms": dict(sorted(algos.items())),
        "resolution": dict(sorted(res.items())),
        "failures": dict(sorted(failures.items())),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
