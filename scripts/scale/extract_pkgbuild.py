#!/usr/bin/env python3
"""Extract Arch Linux source-artifact records (family ``pkgbuild``).

Inputs are the pinned frame only (evidence/scale/frame.json): the Arch Linux
Archive repo databases frame/arch-core.db and frame/arch-extra.db for the frame
date, and the archlinux/packaging/state checkout in CACHE/arch-state.  For every
pkgbase listed in the state repository under core-{x86_64,any} and
extra-{x86_64,any}, the .SRCINFO at the recorded tag commit is fetched from
gitlab.archlinux.org (cached under CACHE/raw/pkgbuild/ so reruns are offline),
its source arrays are paired with the declared checksum arrays, and each source
entry becomes a role-classified artifact.  Pkgbases present in the repo
databases but absent from the state repository are kept as unresolved records.

Writes evidence/scale/pkgbuild.jsonl (v1 schema, sorted by id) and
evidence/scale/pkgbuild-summary.json.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache" / "pkgbridge" / "scale"
STATE = CACHE / "arch-state"
RAW = CACHE / "raw" / "pkgbuild"
OUT = PROJECT / "evidence" / "scale"
FAMILY = "pkgbuild"
AGENT = "pkgbridge-scale (research dataset)"
GITLAB = "https://gitlab.archlinux.org/archlinux/packaging/packages"
STATE_DIRS = {"core-x86_64": "core", "core-any": "core",
              "extra-x86_64": "extra", "extra-any": "extra"}
DBS = {"core": CACHE / "frame" / "arch-core.db", "extra": CACHE / "frame" / "arch-extra.db"}
WORKERS = 6
MIN_INTERVAL = 0.125  # global pacing; GitLab allows 600 unauthenticated web requests/min

# makepkg checksum arrays -> key in "declared"
ALGORITHMS = {"md5sums": "md5", "sha1sums": "sha1", "sha224sums": "sha224",
              "sha256sums": "sha256", "sha384sums": "sha384", "sha512sums": "sha512",
              "b2sums": "blake2b", "cksums": "cksum"}
VCS_PROTOCOLS = {"git", "svn", "hg", "bzr", "fossil"}
TARBALL = re.compile(r"\.(tar(\.(gz|bz2|xz|zst|lz|lz4|lzma|Z))?|tgz|tbz2?|txz|tzst|tlz)$", re.I)
SIGNATURE = re.compile(r"\.(sig|asc|sign)$", re.I)
PATCH = re.compile(r"\.(patch|diff)(\.(gz|bz2|xz|zst))?$", re.I)
VENDOR = re.compile(r"(vendor|node[-_]modules|[-_.]deps[-_.]|crates[-_.]|(^|[-_.])go[-_]mod)", re.I)
PATCH_BUNDLE = re.compile(r"(patches|patchset|patch[-_]?bundle)", re.I)
NAME_PREFIXES = ("python-", "python2-", "perl-", "ruby-", "lua-", "lua51-", "lua52-",
                 "lua53-", "nodejs-", "haskell-", "ocaml-", "php-", "r-", "ttf-", "otf-",
                 "xorg-", "gst-", "lib32-", "rust-", "go-", "emacs-", "vim-")


# ---------------------------------------------------------------- inputs

def git_head() -> str:
    return subprocess.run(["git", "-C", str(PROJECT), "rev-parse", "--short", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_db(repo: str, path: Path) -> dict[str, dict]:
    """pkgbase -> {"versions": set, "names": set, "repo": repo} from a pacman repo db."""
    bases: dict[str, dict] = {}
    with tarfile.open(path) as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith("/desc"):
                continue
            text = tar.extractfile(member).read().decode("utf-8")
            fields: dict[str, list[str]] = {}
            for block in text.strip().split("\n\n"):
                lines = block.strip().split("\n")
                if lines and lines[0].startswith("%") and lines[0].endswith("%"):
                    fields[lines[0].strip("%")] = lines[1:]
            name = fields["NAME"][0]
            base = fields.get("BASE", [name])[0]
            entry = bases.setdefault(base, {"versions": set(), "names": set(), "repo": repo})
            entry["versions"].add(fields["VERSION"][0])
            entry["names"].add(name)
    return bases


def read_state() -> dict[str, list[dict]]:
    """pkgbase -> list of state entries (one per state directory listing it)."""
    entries: dict[str, list[dict]] = defaultdict(list)
    for directory, repo in STATE_DIRS.items():
        for path in sorted((STATE / directory).iterdir()):
            fields = path.read_text(encoding="utf-8").split()
            entry = {"state_file": f"{directory}/{path.name}", "repo": repo,
                     "arch": directory.split("-", 1)[1]}
            if len(fields) == 4 and fields[0] == path.name:
                entry.update(pkgbase=fields[0], version=fields[1], tag=fields[2],
                             commit=fields[3])
            else:
                entry.update(pkgbase=path.name, malformed=" ".join(fields))
            entries[path.name].append(entry)
    return entries


def project_path(pkgbase: str) -> str:
    """GitLab project path for a pkgbase, following devtools gitlab_project_name_to_path."""
    path = re.sub(r"([a-zA-Z0-9]+)\+([a-zA-Z]+)", r"\1-\2", pkgbase)
    path = path.replace("+", "plus")
    path = re.sub(r"[^a-zA-Z0-9_\-.]", "-", path)
    path = re.sub(r"[_\-]{2,}", "-", path)
    return "unix-tree" if path == "tree" else path


# ---------------------------------------------------------------- network

class Fetcher:
    def __init__(self, offline: bool) -> None:
        self.offline = offline
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.network_requests = 0

    def _pace(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = self.next_slot - now
            self.next_slot = max(now, self.next_slot) + MIN_INTERVAL
        if wait > 0:
            time.sleep(wait)

    def _defer(self, seconds: float) -> None:
        with self.lock:
            self.next_slot = max(self.next_slot, time.monotonic() + seconds)

    def _get(self, url: str) -> tuple[int | None, bytes | None, str]:
        """(HTTP status or None on transport failure, body if 200, detail) with retries."""
        detail = ""
        for attempt in range(8):
            self._pace()
            with self.lock:
                self.network_requests += 1
            try:
                request = urllib.request.Request(url, headers={"User-Agent": AGENT})
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.status, response.read(), ""
            except urllib.error.HTTPError as exc:
                detail = f"HTTP {exc.code}"
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After")
                    self._defer(float(retry) if retry and retry.isdigit() else 60.0)
                    continue
                if exc.code < 500:
                    return exc.code, None, detail
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                detail = f"{type(exc).__name__}: {exc}"
            time.sleep(min(2 ** attempt, 60))
        return None, None, detail

    def srcinfo(self, project: str, commit: str) -> dict:
        """Return {"status": "ok", "data": bytes} or {"status": <failure>, "detail": ...}.

        Definitive client errors are cached; when .SRCINFO is missing, the status of
        PKGBUILD at the same commit is recorded to tell a missing file from a bad path.
        """
        url = f"{GITLAB}/{project}/-/raw/{commit}/.SRCINFO"
        body = RAW / project / f"{commit}.SRCINFO"
        failure = RAW / project / f"{commit}.failure.json"
        if body.exists():
            return {"status": "ok", "data": body.read_bytes(), "url": url}
        if failure.exists():
            record = json.loads(failure.read_text())
            if "pkgbuild_http_status" not in record and not self.offline:
                status, _, detail = self._get(f"{GITLAB}/{project}/-/raw/{commit}/PKGBUILD")
                record["pkgbuild_http_status"] = status if status else detail
                failure.write_text(json.dumps(record, indent=2) + "\n")
            return record | {"url": url}
        if self.offline:
            return {"status": "fetch_not_cached", "detail": "offline run", "url": url}
        status, data, detail = self._get(url)
        if status == 200:
            body.parent.mkdir(parents=True, exist_ok=True)
            body.write_bytes(data)
            return {"status": "ok", "data": data, "url": url}
        if status is None:
            return {"status": "fetch_error", "detail": detail, "url": url}
        record = {"status": f"fetch_http_{status}", "detail": detail,
                  "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        probe, _, probe_detail = self._get(f"{GITLAB}/{project}/-/raw/{commit}/PKGBUILD")
        record["pkgbuild_http_status"] = probe if probe else probe_detail
        failure.parent.mkdir(parents=True, exist_ok=True)
        failure.write_text(json.dumps(record, indent=2) + "\n")
        return record | {"url": url}


# ---------------------------------------------------------------- .SRCINFO

def parse_srcinfo(text: str) -> dict:
    base: dict[str, list[str]] = defaultdict(list)
    pkgnames: list[str] = []
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(" = ")
        if not sep:
            key, value = line.rstrip(" ="), ""
        if key == "pkgbase":
            section = "base"
            base["pkgbase"].append(value)
            continue
        if key == "pkgname":
            section = "pkg"
            pkgnames.append(value)
            continue
        if section == "base":
            base[key].append(value)
    return {"base": base, "pkgnames": pkgnames}


def protocol(url: str) -> str | None:
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://", url)
    if not match:
        return None
    scheme = match.group(1).lower()
    if "+" in scheme:
        return scheme.split("+", 1)[0]
    return scheme


def filename_for(entry: str) -> tuple[str, str | None, str | None]:
    """(filename, url-or-None, protocol) as makepkg get_filename/get_protocol resolve them."""
    if "::" in entry:
        name, url = entry.split("::", 1)
    else:
        name, url = None, entry
    proto = protocol(url)
    if proto is None:
        return entry, None, None
    if name is None:
        name = url if proto in VCS_PROTOCOLS else url.rsplit("/", 1)[-1]
    if proto in VCS_PROTOCOLS:
        name = name.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        name = name.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
    return name, url, proto


def vcs_ref(url: str, proto: str) -> dict:
    fragment = url.split("#", 1)[1] if "#" in url else ""
    query = ""
    if "?" in fragment:
        fragment, query = fragment.split("?", 1)
    elif "?" in url.split("#", 1)[0]:
        query = url.split("#", 1)[0].split("?", 1)[1]
    ref = {"type": proto, "fragment": None, "value": None, "signed": "signed" in query.split("&")}
    if fragment:
        kind, _, value = fragment.partition("=")
        ref["fragment"], ref["value"] = kind, value or None
    return ref


def url_path(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url.split("::")[-1]).path
    except ValueError:
        return url


def artifact_kind(name: str, url: str | None, proto: str | None) -> str:
    if url is None:
        return "file"
    if proto in VCS_PROTOCOLS:
        return "vcs"
    path = url_path(url)
    for candidate in (name, path):
        if SIGNATURE.search(candidate):
            return "signature"
    for candidate in (name, path):
        if PATCH.search(candidate):
            return "patch"
    if name.endswith(".crate") or "crates.io/api/v1/crates/" in url or "static.crates.io/crates/" in url:
        return "crate"
    if name.endswith(".gem") or path.endswith(".gem"):
        return "gem"
    for candidate in (name, path):
        if TARBALL.search(candidate):
            return "tarball"
    for candidate in (name, path):
        if candidate.lower().endswith(".zip"):
            return "zip"
    return "other"


def name_tokens(pkgbase: str) -> set[str]:
    tokens = {pkgbase.lower()}
    stem = pkgbase.lower()
    for prefix in NAME_PREFIXES:
        if stem.startswith(prefix) and len(stem) > len(prefix):
            tokens.add(stem[len(prefix):])
    for suffix in ("-git", "-bin"):
        tokens |= {t[: -len(suffix)] for t in list(tokens) if t.endswith(suffix)}
    return {t for t in tokens if len(t) >= 2}


def mentions(text: str, tokens: set[str]) -> bool:
    lowered = text.lower()
    return any(t in lowered for t in tokens)


def residue(name: str, tokens: set[str]) -> str:
    """Filename with pkgbase-derived tokens removed (longest first), for bundle patterns."""
    lowered = name.lower()
    for token in sorted(tokens, key=len, reverse=True):
        lowered = lowered.replace(token, "\0")
    return lowered


def version_tokens(pkgver: str) -> set[str]:
    if not pkgver or len(pkgver) < 2:
        return set()
    return {pkgver.lower(), pkgver.lower().replace(".", "_"), pkgver.lower().replace(".", "-")}


# ---------------------------------------------------------------- records

def source_entries(base: dict[str, list[str]], notes: list[str], failures: Counter) -> list[dict]:
    """Pair every source / source_<arch> entry with its checksum arrays."""
    groups: dict[str | None, list[str]] = {}
    for key, values in base.items():
        if key == "source":
            groups[None] = values
        elif key.startswith("source_"):
            groups[key[len("source_"):]] = values
    entries: list[dict] = []
    for arch in sorted(groups, key=lambda a: (a is not None, a or "")):
        sources = groups[arch]
        suffix = f"_{arch}" if arch else ""
        sums: dict[str, list[str]] = {}
        for array, alg in ALGORITHMS.items():
            values = base.get(array + suffix)
            if values is None:
                continue
            if len(values) != len(sources):
                notes.append(f"{array}{suffix} has {len(values)} entries for "
                             f"{len(sources)} sources; array not paired")
                failures["checksum_count_mismatch"] += 1
                continue
            sums[alg] = values
        for index, entry in enumerate(sources):
            declared, skipped = {}, []
            for alg, values in sums.items():
                value = values[index].strip()
                if value.upper() == "SKIP":
                    skipped.append(alg)
                elif value:
                    declared[alg] = value.lower()
            entries.append({"entry": entry, "arch": arch, "index": index,
                            "declared": declared, "skipped": skipped})
    # checksum arrays without any source array of their arch
    for key in base:
        for array in ALGORITHMS:
            if key == array or key.startswith(array + "_"):
                arch = key[len(array) + 1:] or None
                if arch not in groups:
                    notes.append(f"{key} present without matching source array")
                    failures["checksum_without_source"] += 1
    return entries


def classify(record_id: str, pkgbase: str, pkgver: str, entries: list[dict],
             notes: list[str]) -> tuple[list[dict], bool]:
    artifacts: list[dict] = []
    tokens = name_tokens(pkgbase)
    vtokens = version_tokens(pkgver)
    candidates: list[dict] = []
    for n, item in enumerate(entries):
        name, url, proto = filename_for(item["entry"])
        kind = artifact_kind(name, url, proto)
        art = {"id": f"{record_id}#{n}", "name": name, "url": url, "size": None,
               "kind": kind, "role": None, "role_evidence": None,
               "declared": item["declared"],
               "hash_domain": "vcs" if kind == "vcs" else "file_bytes",
               "condition": f"arch={item['arch']}" if item["arch"] else None,
               "source_index": item["index"], "source_entry": item["entry"]}
        if item["skipped"]:
            art["declared_skip"] = item["skipped"]
        if kind == "vcs":
            art["vcs_ref"] = vcs_ref(url, proto)
        if url is None:
            art["role"] = "packaging"
            art["role_evidence"] = ("local patch (no URL scheme, .patch/.diff name)"
                                    if PATCH.search(name) else
                                    "local file in packaging repository (no URL scheme)")
        elif kind == "signature":
            art["role"] = "signature"
            signed = re.sub(SIGNATURE, "", name)
            art["role_evidence"] = f"signature suffix on '{name}'"
            art["_signs"] = signed
        elif kind == "patch":
            art["role"] = "packaging"
            art["role_evidence"] = "remote patch (.patch/.diff name or URL path)"
        elif kind in {"tarball", "zip", "other"} and PATCH_BUNDLE.search(residue(name, tokens)):
            art["role"] = "patch_bundle"
            art["role_evidence"] = "archive name (outside the pkgbase) mentions patches"
        elif kind in {"tarball", "zip", "other"} and VENDOR.search(residue(name, tokens)):
            art["role"] = "vendor_bundle"
            art["role_evidence"] = ("archive name (outside the pkgbase) matches "
                                    "vendored-dependency pattern")
        else:
            candidates.append(art)
        artifacts.append(art)

    names = {a["name"] for a in artifacts}
    for art in artifacts:
        signed = art.pop("_signs", None)
        if signed is not None:
            if signed in names:
                art["role_evidence"] += f"; signs '{signed}' in the same source array"
            else:
                art["role_evidence"] += "; signed file not found by name in source array"

    def matches_version(art: dict) -> bool:
        text = f"{art['name']} {art['url'] or ''}"
        return bool(vtokens) and mentions(text, vtokens)

    def matches_name(art: dict) -> bool:
        return mentions(f"{art['name']} {art['url'] or ''}", tokens)

    primary = None
    if candidates:
        # Arch-conditional sources only become primary when no unconditional one exists.
        ordered = ([a for a in candidates if a["condition"] is None]
                   or candidates)
        first = ordered[0]
        primary = first
        evidence = "first upstream source in source array (source index %d)" % first["source_index"]
        if first["condition"]:
            evidence += f", only arch-conditional sources ({first['condition']})"
        if not matches_name(first) and not matches_version(first):
            later = next((a for a in ordered[1:] if matches_version(a) and matches_name(a)), None)
            if later is not None:
                primary = later
                evidence = (f"source index {later['source_index']}: first upstream source whose "
                            f"name/URL contains both pkgbase and pkgver; earlier source "
                            f"'{first['name']}' contains neither")
            else:
                evidence += "; name/URL contains neither pkgbase nor pkgver"
        else:
            hits = [w for w, ok in (("pkgbase", matches_name(first)),
                                    ("pkgver", matches_version(first))) if ok]
            evidence += "; name/URL contains " + " and ".join(hits)
        primary["role"] = "primary"
        primary["role_evidence"] = evidence
        for art in candidates:
            if art is primary:
                continue
            if matches_name(art) or matches_version(art):
                art["role"] = "component"
                art["role_evidence"] = ("additional upstream source; name/URL contains "
                                        + ("pkgbase" if matches_name(art) else "pkgver"))
            else:
                art["role"] = "unknown"
                art["role_evidence"] = ("additional remote source; name/URL contains neither "
                                        "pkgbase nor pkgver")
    live = False
    if primary is not None and primary["kind"] == "vcs":
        ref = primary["vcs_ref"]
        if ref["fragment"] not in {"commit", "tag", "revision"} or not ref["value"]:
            live = True
            notes.append("primary VCS source not pinned by commit/tag/revision fragment")
    return artifacts, live


def build_record(pkgbase: str, state: list[dict], db: dict | None, fetched: dict | None,
                 collector: str, failures: Counter) -> dict:
    notes: list[str] = []
    chosen = None
    valid = [e for e in state if "commit" in e]
    for e in state:
        if "malformed" in e:
            notes.append(f"arch-state {e['state_file']} malformed: {e['malformed']!r}")
            failures["state_malformed"] += 1
    if valid:
        db_versions = db["versions"] if db else set()
        matching = [e for e in valid if e["version"] in db_versions]
        chosen = (matching or [e for e in valid if e["arch"] == "x86_64"] or valid)[0]
        for e in valid:
            if e is not chosen:
                notes.append(f"arch-state also lists {e['state_file']} at {e['version']} "
                             f"(commit {e['commit']}); not used")
        if len(valid) > 1:
            failures["duplicate_state_entries"] += 1
    version = chosen["version"] if chosen else sorted(db["versions"])[0]
    record_id = f"{FAMILY}:{pkgbase}@{version}"
    project = project_path(pkgbase)
    record = {"id": record_id, "family": FAMILY, "source_package": pkgbase,
              "version": version, "upstream_version": None, "binary_packages": [],
              "packaging": {"kind": "git", "repository": f"{GITLAB}/{project}.git",
                            "commit": chosen["commit"] if chosen else None,
                            "path": "PKGBUILD", "how_pinned": "arch-state-tag",
                            "metadata_pointer": None, "metadata_sha256": None},
              "artifacts": [], "live_vcs": False, "native": False,
              "resolution": "unresolved", "notes": notes, "collector": collector}
    if chosen:
        notes.append(f"arch-state entry {chosen['state_file']} tag {chosen['tag']}")
    if db is None:
        notes.append("pkgbase absent from frame core.db/extra.db")
        failures["not_in_repo_db"] += 1
    else:
        if len(db["versions"]) > 1:
            notes.append(f"repo db lists several versions: {sorted(db['versions'])}")
            failures["repo_db_multiple_versions"] += 1
        if chosen and chosen["version"] not in db["versions"]:
            notes.append(f"version mismatch: arch-state {chosen['version']} vs "
                         f"{db['repo']}.db {sorted(db['versions'])}")
            failures["version_mismatch_state_db"] += 1
            notes.append("record follows the pinned arch-state entry; the commit for the repo "
                         "db version is not recoverable from the pinned (depth-1) state clone")
        if chosen and db["repo"] != chosen["repo"]:
            notes.append(f"repo mismatch: arch-state {chosen['repo']} vs db {db['repo']}")
            failures["repo_mismatch_state_db"] += 1
    if not chosen:
        notes.append("pkgbase absent from arch-state core/extra; no packaging commit")
        failures["not_in_arch_state"] += 1
        record["upstream_version"] = version.split(":", 1)[-1].rsplit("-", 1)[0]
        record["binary_packages"] = sorted(db["names"]) if db else []
        return record

    record["packaging"]["metadata_pointer"] = fetched["url"]
    if fetched["status"] != "ok":
        notes.append(f".SRCINFO fetch failed: {fetched['status']} ({fetched.get('detail', '')})")
        failures[fetched["status"]] += 1
        probe = fetched.get("pkgbuild_http_status")
        if probe == 200:
            notes.append("PKGBUILD exists at this commit (HTTP 200): the packaging commit has "
                         "no .SRCINFO; sources not extracted (PKGBUILD is not evaluated)")
            failures["srcinfo_absent_pkgbuild_present"] += 1
        elif probe is not None:
            notes.append(f"PKGBUILD at this commit also unavailable ({probe}); project path "
                         "or commit may be wrong")
            failures["srcinfo_and_pkgbuild_unavailable"] += 1
        record["upstream_version"] = version.split(":", 1)[-1].rsplit("-", 1)[0]
        record["binary_packages"] = sorted(db["names"]) if db else []
        return record

    data = fetched["data"]
    record["packaging"]["metadata_sha256"] = hashlib.sha256(data).hexdigest()
    parsed = parse_srcinfo(data.decode("utf-8", errors="replace"))
    base = parsed["base"]
    if not base.get("pkgbase"):
        notes.append(".SRCINFO has no pkgbase line")
        failures["srcinfo_unparsable"] += 1
        record["upstream_version"] = version.split(":", 1)[-1].rsplit("-", 1)[0]
        return record
    if base["pkgbase"][0] != pkgbase:
        notes.append(f".SRCINFO pkgbase {base['pkgbase'][0]!r} differs from state {pkgbase!r}")
        failures["srcinfo_pkgbase_mismatch"] += 1
    pkgver = (base.get("pkgver") or [""])[0]
    pkgrel = (base.get("pkgrel") or [""])[0]
    epoch = (base.get("epoch") or [""])[0]
    srcinfo_version = f"{epoch + ':' if epoch else ''}{pkgver}-{pkgrel}"
    record["upstream_version"] = pkgver
    record["binary_packages"] = parsed["pkgnames"]
    ok = True
    if srcinfo_version != chosen["version"]:
        notes.append(f"version mismatch: .SRCINFO {srcinfo_version} vs arch-state "
                     f"{chosen['version']}")
        failures["version_mismatch_srcinfo_state"] += 1
        ok = False
    if db is not None and chosen["version"] not in db["versions"]:
        ok = False
    if db is not None and set(parsed["pkgnames"]) != db["names"]:
        missing = sorted(db["names"] - set(parsed["pkgnames"]))
        extra = sorted(set(parsed["pkgnames"]) - db["names"])
        notes.append(f"binary package set differs from repo db: db-only {missing}, "
                     f".SRCINFO-only {extra}")
        failures["binary_packages_differ_from_db"] += 1

    before = sum(failures.values())
    entries = source_entries(base, notes, failures)
    if sum(failures.values()) != before:
        ok = False
    artifacts, live = classify(record_id, pkgbase, pkgver, entries, notes)
    record["artifacts"] = artifacts
    record["live_vcs"] = live
    remote = [a for a in artifacts if a["url"] is not None]
    if not remote:
        notes.append("no remote sources: every source entry is a file in the packaging repository")
    for art in remote:
        # Signatures are SKIPped by convention (verified by PGP instead); VCS sources
        # may legitimately lack a tree digest.  Neither makes the record partial.
        if not art["declared"] and art["kind"] not in {"vcs", "signature"}:
            notes.append(f"{art['id']} ({art['name']}) has no declared digest")
            failures["remote_file_without_digest"] += 1
            ok = False
    if remote and not any(a["role"] == "primary" for a in artifacts):
        notes.append("remote sources present but none classified as upstream primary")
    record["resolution"] = "resolved" if ok else "partial"
    return record


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true",
                        help="use only cached .SRCINFO responses")
    parser.add_argument("--limit", type=int, default=None,
                        help="process only the first N pkgbases (debugging)")
    args = parser.parse_args()

    frame = json.loads((OUT / "frame.json").read_text())
    inputs = {}
    for key, repo in (("arch_core_db", "core"), ("arch_extra_db", "extra")):
        digest = sha256_file(DBS[repo])
        if digest != frame["files"][key]["sha256"]:
            sys.exit(f"{DBS[repo]} sha256 {digest} does not match frame.json")
        inputs[key] = {"path": frame["files"][key]["path"], "sha256": digest}
    state_commit = subprocess.run(["git", "-C", str(STATE), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
    if state_commit != frame["repositories"]["arch_state"]["commit"]:
        sys.exit(f"arch-state at {state_commit}, frame pins "
                 f"{frame['repositories']['arch_state']['commit']}")
    inputs["arch_state"] = {"commit": state_commit, "directories": sorted(STATE_DIRS)}

    db: dict[str, dict] = {}
    for repo, path in DBS.items():
        for base, entry in read_db(repo, path).items():
            if base in db:
                db[base]["versions"] |= entry["versions"]
                db[base]["names"] |= entry["names"]
                db[base]["repo"] += "+" + repo
            else:
                db[base] = entry
    state = read_state()
    pkgbases = sorted(set(state) | set(db))
    if args.limit:
        pkgbases = pkgbases[: args.limit]

    collector = f"scripts/scale/extract_pkgbuild.py@{git_head()}"
    fetcher = Fetcher(args.offline)
    jobs = {}
    for base in pkgbases:
        entries = [e for e in state.get(base, []) if "commit" in e]
        if entries:
            versions = db.get(base, {}).get("versions", set())
            chosen = next((e for e in entries if e["version"] in versions), None) or \
                next((e for e in entries if e["arch"] == "x86_64"), entries[0])
            jobs[base] = (project_path(base), chosen["commit"])
    done = [0]
    progress_lock = threading.Lock()

    def fetch(item):
        base, (project, commit) = item
        result = fetcher.srcinfo(project, commit)
        with progress_lock:
            done[0] += 1
            if done[0] % 500 == 0:
                print(f"fetched {done[0]}/{len(jobs)} (network requests "
                      f"{fetcher.network_requests})", file=sys.stderr, flush=True)
        return base, result

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = dict(pool.map(fetch, sorted(jobs.items())))

    failures: Counter = Counter()
    records = [build_record(base, state.get(base, []), db.get(base), fetched.get(base),
                            collector, failures) for base in pkgbases]
    records.sort(key=lambda r: r["id"])
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "pkgbuild.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")

    kinds, roles, kind_role, algs, domains = Counter(), Counter(), Counter(), Counter(), Counter()
    primary_algs, primary_kinds = Counter(), Counter()
    for record in records:
        for art in record["artifacts"]:
            kinds[art["kind"]] += 1
            roles[art["role"]] += 1
            kind_role[f"{art['kind']}/{art['role']}"] += 1
            domains[art["hash_domain"]] += 1
            for alg in art["declared"]:
                algs[alg] += 1
            if art["role"] == "primary":
                primary_kinds[art["kind"]] += 1
                for alg in art["declared"]:
                    primary_algs[alg] += 1
    summary = {
        "family": FAMILY,
        "collector": collector,
        "inputs": inputs,
        "records": len(records),
        "records_in_arch_state": sum(1 for b in pkgbases if b in state),
        "records_in_repo_db": sum(1 for b in pkgbases if b in db),
        "srcinfo_fetched": sum(1 for r in fetched.values() if r["status"] == "ok"),
        "srcinfo_network_requests_this_run": fetcher.network_requests,
        "resolution": dict(sorted(Counter(r["resolution"] for r in records).items())),
        "live_vcs": sum(r["live_vcs"] for r in records),
        "native": sum(r["native"] for r in records),
        "records_with_primary": sum(any(a["role"] == "primary" for a in r["artifacts"])
                                    for r in records),
        "records_without_remote_sources": sum(
            1 for r in records if r["packaging"]["metadata_sha256"]
            and not any(a["url"] for a in r["artifacts"])),
        "artifacts": sum(kinds.values()),
        "artifacts_by_kind": dict(sorted(kinds.items())),
        "artifacts_by_role": dict(sorted(roles.items())),
        "artifacts_by_kind_role": dict(sorted(kind_role.items())),
        "artifacts_by_hash_domain": dict(sorted(domains.items())),
        "hash_algorithms_present": dict(sorted(algs.items())),
        "primary_by_kind": dict(sorted(primary_kinds.items())),
        "primary_hash_algorithms": dict(sorted(primary_algs.items())),
        "failures_by_category": dict(sorted(failures.items())),
    }
    (OUT / "pkgbuild-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
