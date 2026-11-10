#!/usr/bin/env python3
"""Extract Gentoo ebuild source artifacts for the scaled dataset (v1 schema).

Offline. Reads the pinned gentoo-mirror checkout recorded in
evidence/scale/frame.json: one record per metadata/md5-cache/<cat>/<pkg>-<ver>
entry. The cache SRC_URI is already variable-expanded; it is parsed for USE /
arch conditionals, '->' renames, and URI prefixes, and every distfile is
joined to the DIST line of <cat>/<pkg>/Manifest with the same name (only the
distfiles this ebuild references). Only digests declared in the Manifest are
recorded. Live ebuilds keep a VCS artifact when the ebuild assigns a literal
repository URI.

Writes evidence/scale/ebuild.jsonl (sorted by id) and
evidence/scale/ebuild-summary.json.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

PROJECT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache" / "pkgbridge" / "scale"
REPOSITORY = "https://github.com/gentoo-mirror/gentoo"
FAMILY = "ebuild"

VERSION_RE = re.compile(r"^\d+(\.\d+)*[a-z]?(_(alpha|beta|pre|rc|p)\d*)*(-r\d+)?$")
REVISION_RE = re.compile(r"-r\d+$")
LIVE_VERSION_RE = re.compile(r"(^|\.)9999(-r\d+)?$")
VCS_ECLASSES = ("git-r3", "mercurial", "subversion", "cvs", "bzr", "darcs")
VCS_ASSIGN_RE = re.compile(r"^[ \t]*(?:local[ \t]+)?(EGIT_REPO_URI|EHG_REPO_URI|ESVN_REPO_URI)=", re.M)
HASH_KEYS = {"BLAKE2B": "blake2b", "SHA512": "sha512", "SHA256": "sha256", "MD5": "md5",
             "SHA1": "sha1", "RMD160": "rmd160", "WHIRLPOOL": "whirlpool", "SHA3_256": "sha3_256",
             "SHA3_512": "sha3_512", "STREEBOG256": "streebog256", "STREEBOG512": "streebog512"}
TAR_SUFFIXES = (".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz", ".tar.lzma",
                ".tar.lz4", ".tar.Z", ".tgz", ".tbz", ".tbz2", ".txz", ".tzst", ".tar.bz")
ZIP_SUFFIXES = (".zip", ".ZIP")
SIG_SUFFIXES = (".asc", ".sig", ".sign", ".provenance")
PATCH_SUFFIXES = (".patch", ".diff", ".patch.gz", ".patch.bz2", ".patch.xz", ".diff.gz",
                  ".diff.bz2", ".diff.xz", ".patch.zst")
OTHER_ARCHIVE_SUFFIXES = (".7z", ".rar", ".deb", ".rpm", ".whl", ".jar", ".xpi", ".AppImage",
                          ".appimage", ".exe", ".msi", ".dmg", ".cpio", ".cpio.gz", ".lha", ".lzh",
                          ".shar", ".run", ".bin", ".iso", ".nupkg")
COMPRESSED_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".lz", ".lzma", ".Z")


def git_head(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def split_name_version(cat: str, name: str, repo: Path) -> tuple[str, str] | None:
    """Split '<pkg>-<ver>' at the rightmost hyphen whose suffix is a PMS version."""
    parts = name.split("-")
    for i in range(len(parts) - 1, 0, -1):
        pn, pv = "-".join(parts[:i]), "-".join(parts[i:])
        if VERSION_RE.match(pv) and (repo / cat / pn).is_dir():
            return pn, pv
    for i in range(len(parts) - 1, 0, -1):
        pn, pv = "-".join(parts[:i]), "-".join(parts[i:])
        if VERSION_RE.match(pv):
            return pn, pv
    return None


def parse_cache(path: Path) -> dict[str, str]:
    fields = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    return fields


def parse_src_uri(value: str) -> tuple[list[dict], list[str]]:
    """Return distfile occurrences in order and parse errors.

    Each occurrence: {"name", "url", "raw_uri", "condition"}; condition is the
    space-joined stack of enclosing 'flag?' groups, or None. When one distfile
    occurs under several conditions, build_record joins them with ' || '.
    """
    tokens = value.split()
    stack: list[str | None] = []
    occurrences: list[dict] = []
    errors: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.endswith("?") and i + 1 < len(tokens) and tokens[i + 1] == "(":
            stack.append(tok)
            i += 2
            continue
        if tok == "(":
            stack.append(None)
            i += 1
            continue
        if tok == ")":
            if stack:
                stack.pop()
            else:
                errors.append("unbalanced ')'")
            i += 1
            continue
        if tok == "->" or tok.endswith("?") or tok == "||":
            errors.append(f"unexpected token {tok!r}")
            i += 1
            continue
        if i + 1 < len(tokens) and tokens[i + 1] == "->":
            if i + 2 >= len(tokens):
                errors.append(f"'->' without target after {tok!r}")
                i += 2
                continue
            name = tokens[i + 2]
            i += 3
        else:
            name = tok.rsplit("/", 1)[-1] if "://" in tok else tok
            i += 1
        conds = [c for c in stack if c]
        url = None
        if "://" in tok:
            url = tok
            for prefix in ("fetch+", "mirror+"):
                if url.startswith(prefix):
                    url = url[len(prefix):]
        occurrences.append({"name": name, "url": url, "raw_uri": tok if "://" in tok else None,
                            "condition": " ".join(conds) if conds else None})
    if stack:
        errors.append(f"{len(stack)} unclosed group(s)")
    return occurrences, errors


def parse_manifest(path: Path) -> tuple[dict[str, dict], list[str]]:
    dist: dict[str, dict] = {}
    errors: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        parts = line.split()
        if not parts or parts[0] != "DIST":
            continue
        if len(parts) < 3 or (len(parts) - 3) % 2:
            errors.append(f"malformed DIST line {lineno}")
            continue
        name, size = parts[1], parts[2]
        hashes = {}
        for algo, digest in zip(parts[3::2], parts[4::2]):
            hashes[HASH_KEYS.get(algo, algo.lower())] = digest.lower()
        try:
            size_int = int(size)
        except ValueError:
            errors.append(f"non-integer size on DIST line {lineno}")
            size_int = None
        if name in dist and (dist[name]["size"] != size_int or dist[name]["declared"] != hashes):
            errors.append(f"conflicting duplicate DIST {name}")
        dist[name] = {"size": size_int, "declared": hashes}
    return dist, errors


def artifact_kind(name: str) -> str:
    if name.endswith(SIG_SUFFIXES):
        return "signature"
    if name.endswith(TAR_SUFFIXES):
        return "tarball"
    if name.endswith(ZIP_SUFFIXES):
        return "zip"
    if name.endswith(".crate"):
        return "crate"
    if name.endswith(".gem"):
        return "gem"
    if name.endswith(PATCH_SUFFIXES):
        return "patch"
    if name.endswith(OTHER_ARCHIVE_SUFFIXES):
        return "other"
    return "file"


def is_go_module_file(name: str) -> bool:
    return "%2F@v%2F" in name or "/@v/" in name or name.endswith((".mod", ".gomod"))


PATCH_WORD_RE = re.compile(r"(?<![a-z])patch")


def classify(artifacts: list[dict], pn: str, pv_upstream: str, live: bool) -> None:
    """Assign role and role_evidence in place (SRC_URI order preserved).

    For live ebuilds the VCS checkout is the main source, so no distfile is
    promoted to primary even when the repository URI could not be extracted.
    """
    crates = sum(1 for a in artifacts if a["kind"] == "crate")
    nupkgs = sum(1 for a in artifacts if a["name"].endswith(".nupkg"))
    own_crate = f"{pn}-{pv_upstream}.crate"
    pn_re = re.compile(re.escape(pn), re.I) if pn else None
    pn_norm = normalise(pn)
    candidates: list[dict] = []
    for a in artifacts:
        name = a["name"]
        # Keywords must survive removal of the package name both literally and
        # with separators dropped, so that e.g. dev-util/patchelf,
        # app-emulation/vendor-reset, or dev-python/jsonpatch
        # (python-json-patch-*.tar.gz) keep their own archive.
        stripped = pn_re.sub("", name).lower() if pn_re else name.lower()
        flat = normalise(name).replace(pn_norm, "") if pn_norm else normalise(name)
        url = (a["url"] or "").lower()
        if a["kind"] == "vcs":
            a["role"], a["role_evidence"] = "primary", "live ebuild VCS repository URI assigned in the ebuild"
            continue
        if a["kind"] == "signature":
            suffix = next(s for s in SIG_SUFFIXES if name.endswith(s))
            a["role"], a["role_evidence"] = "signature", f"filename suffix {suffix}"
            continue
        if a["kind"] == "crate" and crates >= 2 and name != own_crate:
            a["role"], a["role_evidence"] = "vendor_bundle", f"one of {crates} .crate distfiles in SRC_URI"
            continue
        if name.endswith(".nupkg") and nupkgs >= 2:
            a["role"], a["role_evidence"] = "vendor_bundle", f"one of {nupkgs} NuGet .nupkg distfiles in SRC_URI"
            continue
        if a["kind"] == "patch":
            a["role"], a["role_evidence"] = "patch_bundle", "filename patch/diff suffix"
            continue
        if PATCH_WORD_RE.search(stripped) and "patch" in flat:
            ev = "filename contains '-patches-'" if "-patches-" in stripped else "filename contains 'patch'"
            a["role"], a["role_evidence"] = "patch_bundle", ev
            continue
        if "-patches/" in url or "/patches/" in url:
            a["role"], a["role_evidence"] = "patch_bundle", "URI directory named patches"
            continue
        keyword = next((k for k in ("vendor", "-deps", "crates", "node_modules", "go-mod")
                        if k in stripped and normalise(k) in flat), None)
        if keyword:
            a["role"], a["role_evidence"] = "vendor_bundle", f"filename contains {keyword!r}"
            continue
        if is_go_module_file(name):
            a["role"], a["role_evidence"] = "vendor_bundle", "Go module proxy file (EGO_SUM style name)"
            continue
        archive = a["kind"] in ("tarball", "zip", "crate", "gem") or (
            a["kind"] == "file" and name.endswith(COMPRESSED_SUFFIXES))
        if not archive:
            a["role"], a["role_evidence"] = "unknown", f"non-archive distfile (kind {a['kind']})"
            continue
        a["role"] = "component"
        if live:
            a["role_evidence"] = "archive in a live ebuild (VCS checkout is the main source)"
        elif a["condition"] is not None:
            a["role_evidence"] = "conditional archive in SRC_URI"
        else:
            a["role_evidence"] = "unconditional archive in SRC_URI not selected as primary"
            candidates.append(a)
    if candidates:
        named = [a for a in candidates if pn_norm and pn_norm in normalise(a["name"])]
        if named:
            named[0]["role"] = "primary"
            named[0]["role_evidence"] = ("first unconditional upstream archive in SRC_URI whose name "
                                         "contains the package name")
        else:
            candidates[0]["role"] = "primary"
            candidates[0]["role_evidence"] = ("first unconditional upstream archive in SRC_URI "
                                              "(no archive name contains the package name)")


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def vcs_uris(ebuild: Path, pn: str, pv: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Literal repository URIs assigned in a live ebuild (after ${PN}/${P}/${PV})."""
    text = ebuild.read_text(encoding="utf-8", errors="replace")
    found: list[tuple[str, str]] = []
    unexpanded: list[str] = []
    subst = {"${PN}": pn, "$PN": pn, "${P}": f"{pn}-{pv}", "${PV}": pv}
    for m in VCS_ASSIGN_RE.finditer(text):
        var, rest = m.group(1), text[m.end():]
        if rest.startswith("("):
            value = rest[1:rest.find(")")] if ")" in rest else ""
        elif rest[:1] in ("'", '"'):
            end = rest.find(rest[0], 1)
            value = rest[1:end] if end > 0 else ""
        else:
            value = rest.split(None, 1)[0] if rest.split(None, 1) else ""
        tokens = [t.strip("'\"") for t in value.split()]
        first = tokens[0] if tokens else ""
        for k, v in subst.items():
            first = first.replace(k, v)
        if "$" in first or not first:
            unexpanded.append(f"{var}={value.strip()}")
        elif (var, first) not in found:
            found.append((var, first))
    return found, unexpanded


def build_record(cache_file: Path, repo: Path, commit: str, collector: str) -> tuple[dict, Counter]:
    failures: Counter = Counter()
    cat = cache_file.parent.name
    pf = cache_file.name
    notes: list[str] = []
    split = split_name_version(cat, pf, repo)
    if split is None:
        failures["name_version_split_failed"] += 1
        pn, pv = pf, ""
        notes.append("failure name_version_split_failed: cache name has no PMS version suffix")
    else:
        pn, pv = split
    upstream = REVISION_RE.sub("", pv)
    record_id = f"{FAMILY}:{cat}/{pn}@{pv}"
    fields = parse_cache(cache_file)
    ebuild_rel = f"{cat}/{pn}/{pf}.ebuild"
    ebuild = repo / ebuild_rel
    if not ebuild.is_file():
        failures["ebuild_missing"] += 1
        notes.append(f"failure ebuild_missing: {ebuild_rel} not in checkout")

    live_evidence = []
    if LIVE_VERSION_RE.search(pv):
        live_evidence.append("version 9999")
    if "live" in fields.get("PROPERTIES", "").split():
        live_evidence.append("PROPERTIES live")
    inherits = fields.get("INHERIT", "").split()
    for ecl in VCS_ECLASSES:
        if ecl in inherits:
            live_evidence.append(f"inherit {ecl}")
    live = bool(live_evidence)
    if live:
        notes.append("live_vcs: " + ", ".join(live_evidence))

    restrict = fields.get("RESTRICT", "").split()
    for flag in ("fetch", "mirror"):
        if flag in restrict:
            notes.append(f"RESTRICT {flag} (possibly USE-conditional): {fields['RESTRICT']}")

    occurrences, parse_errors = parse_src_uri(fields.get("SRC_URI", ""))
    for err in parse_errors:
        failures["src_uri_parse_error"] += 1
        notes.append(f"failure src_uri_parse_error: {err}")

    merged: dict[str, dict] = {}
    order: list[str] = []
    for occ in occurrences:
        entry = merged.get(occ["name"])
        if entry is None:
            merged[occ["name"]] = {"name": occ["name"], "url": occ["url"], "urls": [occ["url"]],
                                   "conditions": [occ["condition"]]}
            order.append(occ["name"])
        else:
            if occ["url"] not in entry["urls"]:
                entry["urls"].append(occ["url"])
            if occ["condition"] not in entry["conditions"]:
                entry["conditions"].append(occ["condition"])
            if entry["url"] is None and occ["url"] is not None:
                entry["url"] = occ["url"]

    manifest_path = repo / cat / pn / "Manifest"
    dist: dict[str, dict] = {}
    if order:
        if manifest_path.is_file():
            dist, manifest_errors = parse_manifest(manifest_path)
            for err in manifest_errors:
                failures["manifest_parse_error"] += 1
                notes.append(f"failure manifest_parse_error: {err}")
        else:
            failures["manifest_missing"] += 1
            notes.append(f"failure manifest_missing: {cat}/{pn}/Manifest")

    artifacts: list[dict] = []
    for name in order:
        entry = merged[name]
        conds = entry["conditions"]
        condition = None if None in conds else " || ".join(c for c in conds if c)
        art = {"name": name, "url": entry["url"], "size": None, "kind": artifact_kind(name),
               "role": None, "role_evidence": None, "declared": {}, "hash_domain": "file_bytes",
               "condition": condition}
        alt = [u for u in entry["urls"] if u is not None and u != entry["url"]]
        if alt:
            notes.append(f"alternate URIs for {name}: " + " ".join(alt))
        if entry["url"] is None:
            notes.append(f"no URI for distfile {name} (bare filename in SRC_URI)")
        d = dist.get(name)
        if d is not None:
            art["size"] = d["size"]
            art["declared"] = d["declared"]
        elif manifest_path.is_file():
            failures["distfile_not_in_manifest"] += 1
            notes.append(f"failure distfile_not_in_manifest: {name}")
        artifacts.append(art)

    missing_vcs = 0
    if live:
        if ebuild.is_file():
            found, unexpanded = vcs_uris(ebuild, pn, pv)
        else:
            found, unexpanded = [], []
        for var, uri in found:
            artifacts.append({"name": uri.rstrip("/").rsplit("/", 1)[-1] or uri, "url": uri, "size": None,
                              "kind": "vcs", "role": None, "role_evidence": None, "declared": {},
                              "hash_domain": "vcs", "condition": None})
            notes.append(f"vcs artifact from {var}; no commit pinned (live ebuild follows branch head)")
        for u in unexpanded:
            notes.append(f"unexpanded VCS URI not recorded: {u}")
        if not found:
            missing_vcs = 1
            failures["live_vcs_uri_unextracted"] += 1
            notes.append("failure live_vcs_uri_unextracted: no literal EGIT/EHG/ESVN_REPO_URI in ebuild "
                         "(set by an eclass or via variables)")

    classify(artifacts, pn, upstream, live)
    for n, art in enumerate(artifacts):
        art["id"] = f"{record_id}#{n}"
    artifacts = [{k: a[k] for k in ("id", "name", "url", "size", "kind", "role", "role_evidence",
                                    "declared", "hash_domain", "condition")} for a in artifacts]

    # Every artifact, plus an unrecorded VCS checkout of a live ebuild, needs a
    # declared digest; VCS artifacts never have one (no commit is pinned).
    needed = len(artifacts) + missing_vcs
    ok = sum(1 for a in artifacts if a["declared"])
    if needed == 0:
        resolution = "resolved"
        notes.append("no SRC_URI: ebuild declares no distfiles")
    else:
        resolution = "resolved" if ok == needed else ("partial" if ok else "unresolved")

    cache_rel = str(cache_file.relative_to(repo))
    record = {
        "id": record_id, "family": FAMILY, "source_package": f"{cat}/{pn}", "version": pv,
        "upstream_version": upstream, "binary_packages": [],
        "packaging": {"kind": "git", "repository": REPOSITORY, "commit": commit, "path": ebuild_rel,
                      "how_pinned": "gentoo-commit", "metadata_pointer": cache_rel,
                      "metadata_sha256": hashlib.sha256(cache_file.read_bytes()).hexdigest()},
        "artifacts": artifacts, "live_vcs": live, "native": False, "resolution": resolution,
        "notes": notes, "collector": collector,
    }
    return record, failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="process only the first N cache entries (debug)")
    args = parser.parse_args()
    frame = json.loads((PROJECT / "evidence" / "scale" / "frame.json").read_text())
    commit = frame["repositories"]["gentoo"]["commit"]
    repo = CACHE / "gentoo-mirror"
    head = git_head(repo)
    if head != commit:
        raise SystemExit(f"{repo} is at {head}, frame pins {commit}")
    collector = "scripts/scale/extract_ebuild.py@" + subprocess.check_output(
        ["git", "-C", str(PROJECT), "rev-parse", "--short", "HEAD"], text=True).strip()

    cache_files = sorted(p for p in (repo / "metadata" / "md5-cache").glob("*/*") if p.is_file())
    if args.limit:
        cache_files = cache_files[:args.limit]
    records = []
    failures: Counter = Counter()
    for cf in cache_files:
        rec, fails = build_record(cf, repo, commit, collector)
        records.append(rec)
        failures.update(fails)
    records.sort(key=lambda r: r["id"])
    ids = Counter(r["id"] for r in records)
    dup = {k: v for k, v in ids.items() if v > 1}

    kinds, roles, kind_role, algos, res, conds = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    url_schemes, primary_evidence = Counter(), Counter()
    undeclared = 0
    records_with_primary = records_live = records_no_artifacts = 0
    records_failed = 0
    for r in records:
        res[r["resolution"]] += 1
        records_live += r["live_vcs"]
        records_no_artifacts += not r["artifacts"]
        records_with_primary += any(a["role"] == "primary" and a["kind"] != "vcs" for a in r["artifacts"])
        records_failed += any(n.startswith("failure ") for n in r["notes"])
        for a in r["artifacts"]:
            kinds[a["kind"]] += 1
            roles[a["role"]] += 1
            kind_role[f"{a['kind']}/{a['role']}"] += 1
            for k in a["declared"]:
                algos[k] += 1
            conds["conditional" if a["condition"] else "unconditional"] += 1
            undeclared += not a["declared"]
            if a["role"] == "primary":
                primary_evidence[a["role_evidence"]] += 1
            url_schemes[a["url"].split("://", 1)[0] + "://" if a["url"] else "null"] += 1
    summary = {
        "family": FAMILY, "collector": collector,
        "inputs": {"repository": REPOSITORY, "commit": commit, "cache_entries": len(cache_files)},
        "records": len(records), "duplicate_ids": dup,
        "records_live_vcs": records_live, "records_without_artifacts": records_no_artifacts,
        "records_with_file_primary": records_with_primary,
        "records_with_failure_notes": records_failed,
        "artifacts": sum(kinds.values()),
        "artifacts_by_kind": dict(sorted(kinds.items())),
        "artifacts_by_role": dict(sorted(roles.items())),
        "artifacts_by_kind_role": dict(sorted(kind_role.items())),
        "artifacts_by_condition": dict(sorted(conds.items())),
        "artifacts_without_declared_digest": undeclared,
        "primary_selection_evidence": dict(sorted(primary_evidence.items())),
        "artifact_url_schemes": dict(sorted(url_schemes.items())),
        "hash_algorithms_present": dict(sorted(algos.items())),
        "resolution": dict(sorted(res.items())),
        "failures_by_category": dict(sorted(failures.items())),
    }
    out_dir = PROJECT / "evidence" / "scale"
    with (out_dir / f"{FAMILY}.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n")
    (out_dir / f"{FAMILY}-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
