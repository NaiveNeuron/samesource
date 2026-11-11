#!/usr/bin/env python3
"""Extract Fedora rawhide source packages into v1 records (evidence/scale/rpm.jsonl).

Reads the pinned compose source repodata (frame/fedora-primary.xml.zst), pins
each SRPM's packaging revision through Koji build provenance (getBuild(NVR)
'source' = git+https://src.fedoraproject.org/rpms/<name>.git#<commit>, queried
in system.multicall batches), then reads the dist-git 'sources' file at that
commit. Every declared lookaside digest becomes an artifact with a role
classified from filename evidence. Raw Koji and dist-git responses are cached
under CACHE/raw/rpm/ so reruns are offline; failures stay in the records.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xmlrpc.client

PROJECT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache" / "pkgbridge" / "scale"
RAW = CACHE / "raw" / "rpm"
AGENT = "pkgbridge-scale (research dataset)"
KOJI_HUB = "https://koji.fedoraproject.org/kojihub"
DISTGIT = "https://src.fedoraproject.org"
LOOKASIDE = "https://src.fedoraproject.org/repo/pkgs/rpms"
MAX_WORKERS = 6
NS = {"c": "http://linux.duke.edu/metadata/common"}

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SOURCE_RE = re.compile(r"^(?:git\+)?(?P<repo>[a-z]+://[^#?]+?)(?:\?)?#(?P<ref>[^#]+)$")
NEW_LINE_RE = re.compile(r"^(?P<algo>[A-Za-z0-9-]+) \((?P<name>.+)\) = (?P<hex>[0-9a-fA-F]+)$")
OLD_LINE_RE = re.compile(r"^(?P<hex>[0-9a-fA-F]{32})\s+(?P<name>\S.*)$")
HEX_LEN = {"md5": 32, "sha1": 40, "sha224": 56, "sha256": 64, "sha384": 96, "sha512": 128}

TAR_RE = re.compile(r"\.(tar(\.(gz|bz2|xz|zst|zstd|lz|lzma|lz4|Z))?|tgz|tbz2?|txz|tlz|tzst)$", re.I)
SIG_RE = re.compile(r"\.(asc|sig|sign|gpg|pgp)$", re.I)
PATCH_RE = re.compile(r"\.(patch|diff)(\.(gz|bz2|xz|zst))?$", re.I)
OTHER_ARCHIVE_RE = re.compile(r"\.(7z|rar|jar|war|whl|egg|xpi|oxt|vsix|nupkg|rpm|deb|cpio|zst|xz|bz2|gz|lz|lzma)$", re.I)
VENDOR_PATTERNS = (
    (re.compile(r"vendor", re.I), "filename contains 'vendor'"),
    (re.compile(r"(^|[-_.])crates?[-_.]", re.I), "filename contains 'crate(s)' (cargo crate bundle)"),
    (re.compile(r"-nm-(prod|dev)\.", re.I), "filename has '-nm-prod'/'-nm-dev' (node_modules bundle)"),
    (re.compile(r"node[-_]modules", re.I), "filename contains 'node_modules'"),
)

_tls = threading.local()


def git_head() -> str:
    return subprocess.check_output(["git", "-C", str(PROJECT), "rev-parse", "--short", "HEAD"],
                                   text=True).strip()


# ---------------------------------------------------------------- frame

def read_frame(path: Path) -> list[dict]:
    data = subprocess.run(["zstd", "-dc", str(path)], check=True, capture_output=True).stdout
    root = ET.fromstring(data)
    out = []
    for pkg in root.findall("c:package", NS):
        if pkg.findtext("c:arch", namespaces=NS) != "src":
            continue
        v = pkg.find("c:version", NS)
        out.append({
            "name": pkg.findtext("c:name", namespaces=NS),
            "epoch": v.get("epoch") or "0",
            "ver": v.get("ver"),
            "rel": v.get("rel"),
            "location": pkg.find("c:location", NS).get("href"),
            "pkgid": pkg.findtext("c:checksum", namespaces=NS),
        })
    return out


# ---------------------------------------------------------------- koji

def koji_cache_path(nvr: str) -> Path:
    digest = hashlib.sha256(nvr.encode()).hexdigest()
    return RAW / "koji" / digest[:2] / f"{nvr}.json"


def koji_batch(nvrs: list[str]) -> None:
    """Fetch getBuild for nvrs via one system.multicall and cache each answer."""
    proxy = xmlrpc.client.ServerProxy(KOJI_HUB, allow_none=True,
                                      transport=_UATransport(use_datetime=False))
    calls = [{"methodName": "getBuild", "params": [nvr]} for nvr in nvrs]
    for attempt in range(6):
        try:
            res = proxy.system.multicall(calls)
            break
        except (OSError, xmlrpc.client.ProtocolError, TimeoutError) as exc:
            code = getattr(exc, "errcode", None)
            if code is not None and code < 500 and code != 429 or attempt == 5:
                raise
            time.sleep(5 * 2 ** attempt)
    for nvr, item in zip(nvrs, res):
        if isinstance(item, dict) and "faultCode" in item:
            payload = {"fault": {"code": item["faultCode"], "string": item["faultString"]}}
        else:
            payload = {"build": item[0]}
        p = koji_cache_path(nvr)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, json.dumps(payload, sort_keys=True, default=str))


class _UATransport(xmlrpc.client.SafeTransport):
    user_agent = AGENT

    def make_connection(self, host):  # noqa: D401 - set a timeout on the connection
        conn = super().make_connection(host)
        conn.timeout = 180
        return conn


def koji_lookup_all(nvrs: list[str], batch: int, workers: int) -> dict[str, dict]:
    missing = [n for n in nvrs if not koji_cache_path(n).exists()]
    batches = [missing[i:i + batch] for i in range(0, len(missing), batch)]
    errors: dict[str, str] = {}
    if batches:
        print(f"koji: {len(missing)} NVRs uncached, {len(batches)} multicall batches", file=sys.stderr)

        def run(b):
            try:
                koji_batch(b)
            except Exception as exc:  # keep visible per record
                for n in b:
                    errors[n] = f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, _ in enumerate(ex.map(run, batches), 1):
                if i % 20 == 0:
                    print(f"koji: {i}/{len(batches)} batches", file=sys.stderr)
    out = {}
    for n in nvrs:
        p = koji_cache_path(n)
        if p.exists():
            out[n] = json.loads(p.read_text())
        else:
            out[n] = {"error": errors.get(n, "not fetched")}
    return out


# ---------------------------------------------------------------- dist-git

def http_get(url: str) -> tuple[int, bytes]:
    """GET with retries on 5xx/429/timeouts; returns (status, body) for 2xx and 4xx."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    last: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                return exc.code, exc.read()
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(3 * 2 ** attempt)
    raise RuntimeError(f"{url}: {type(last).__name__}: {last}")


def cached_get(url: str, key: Path) -> dict:
    """Cached GET. Only definitive answers (2xx, 4xx) are cached."""
    p = RAW / key
    if p.exists():
        return json.loads(p.read_text())
    status, body = http_get(url)
    payload = {"url": url, "status": status,
               "body": body.decode("utf-8", "replace") if status == 200 else None,
               "body_sha256": hashlib.sha256(body).hexdigest() if status == 200 else None}
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, json.dumps(payload, sort_keys=True))
    return payload


def atomic_write(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)


def fetch_sources(repo_name: str, commit: str) -> dict:
    """Return {'state': present|absent|missing_commit|error, ...} for the sources file."""
    base = f"{DISTGIT}/rpms/{urllib.parse.quote(repo_name)}/raw/{commit}/f"
    try:
        src = cached_get(f"{base}/sources", Path("distgit") / repo_name / f"{commit}.sources.json")
        if src["status"] == 200:
            return {"state": "present", **src}
        if src["status"] != 404:
            return {"state": "error", "detail": f"sources HTTP {src['status']}", **src}
        spec = cached_get(f"{base}/{urllib.parse.quote(repo_name)}.spec",
                          Path("distgit") / repo_name / f"{commit}.spec-probe.json")
        spec.pop("body", None)
        if spec["status"] == 200:
            return {"state": "absent", "url": src["url"], "spec_probe": spec["url"]}
        return {"state": "missing_commit", "url": src["url"], "spec_probe": spec["url"],
                "detail": f"sources 404 and spec HTTP {spec['status']} at commit"}
    except Exception as exc:
        return {"state": "error", "url": f"{base}/sources", "detail": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- classification

def parse_sources(text: str) -> tuple[list[dict], list[str]]:
    entries, bad = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = NEW_LINE_RE.match(line)
        if m:
            algo = m["algo"].lower().replace("-", "")
            hexd = m["hex"].lower()
            if algo in HEX_LEN and len(hexd) != HEX_LEN[algo]:
                bad.append(line)
                continue
            entries.append({"name": m["name"], "algo": algo, "hex": hexd, "format": "tagged"})
            continue
        m = OLD_LINE_RE.match(line)
        if m:
            entries.append({"name": m["name"].strip(), "algo": "md5", "hex": m["hex"].lower(),
                            "format": "legacy-md5"})
            continue
        bad.append(line)
    return entries, bad


def kind_of(name: str) -> str:
    low = name.lower()
    if SIG_RE.search(low) or ("keyring" in low and not (TAR_RE.search(name) or low.endswith(".zip"))):
        return "signature"
    if low.endswith(".crate"):
        return "crate"
    if low.endswith(".gem"):
        return "gem"
    if PATCH_RE.search(low):
        return "patch"
    if TAR_RE.search(name):
        return "tarball"
    if low.endswith(".zip"):
        return "zip"
    if OTHER_ARCHIVE_RE.search(low):
        return "other"
    return "file"


def norm(s: str) -> str:
    return re.sub(r"[-_.~^+]+", "-", s.lower()).strip("-")


NAME_PREFIXES = ("python3-", "python-", "perl-", "rubygem-", "ruby-", "nodejs-", "ghc-", "R-",
                 "php-pecl-", "php-pear-", "php-", "ocaml-", "rust-", "golang-", "texlive-",
                 "mingw32-", "mingw64-", "mingw-", "lua-", "erlang-", "js-", "tcl-", "gap-pkg-",
                 "octave-", "emacs-", "vim-", "gnome-shell-extension-", "xorg-x11-drv-",
                 "xorg-x11-", "java-", "maven-", "trytond-", "pypy-", "julia-")
AUX_RE = re.compile(r"(^|-)(docs?|manual|html|data|tests?|testdata|examples?|generated|l10n|"
                    r"translations?|langpacks?|fonts?|man|images?|assets|po|i18n)(-|$)")
ARCHIVE_KINDS = ("tarball", "zip", "gem", "other")


def name_stems(pkg: str) -> list[str]:
    """Normalised package-name stems (full, prefix-stripped, trailing digits dropped)."""
    cands = [pkg]
    for p in NAME_PREFIXES:
        if pkg.startswith(p) and len(pkg) > len(p):
            rest = pkg[len(p):]
            cands.append(rest)
            if p == "golang-":
                cands.append(rest.rsplit("-", 1)[-1])
            break
    out = []
    for c in cands:
        for s in (norm(c), norm(re.sub(r"\d+$", "", c))):
            if len(s) >= 3 and s not in out:
                out.append(s)
    return out


def crate_name(fname: str) -> str | None:
    m = re.match(r"^(?P<n>.+?)-(?P<v>\d[^-]*(?:-[^-]+)*)\.crate$", fname)
    return m["n"] if m else None


def own_crate_name(pkg: str, upstream: str, cn: str) -> str | None:
    """Why crate cn is the package's own crate (rust-<crate> or compat rust-<crate><major>), else None."""
    rest = norm(pkg[5:])
    if norm(cn) == rest:
        return f"crate '{cn}' matches package name {pkg}"
    base = norm(cn)
    if rest.startswith(base):
        suffix = rest[len(base):].lstrip("-")
        if suffix and norm(upstream).startswith(suffix) and (
                len(norm(upstream)) == len(suffix) or norm(upstream)[len(suffix)] == "-"):
            return f"crate '{cn}' matches compat package name {pkg} (version series {suffix})"
    return None


EXT_RE = re.compile(r"\.(tar(\.(gz|bz2|xz|zst|zstd|lz|lzma|lz4|Z))?|tgz|tbz2?|txz|tlz|tzst|zip|gem|crate|"
                    r"7z|rar|jar|war|whl|egg|xpi|oxt|vsix|nupkg|rpm|deb|cpio|zst|xz|bz2|gz|lz|lzma|"
                    r"pdf|txt|dat|json|bin|sha\d+|md5)$", re.I)


def strip_ext(name: str) -> str:
    for _ in range(2):  # e.g. GeoIP.dat.gz
        name = EXT_RE.sub("", name)
    return name


def classify(pkg: str, upstream: str, entries: list[dict]) -> list[tuple[str, str, str]]:
    """Return (kind, role, role_evidence) per entry, in sources-file order."""
    out: list[tuple[str, str, str] | None] = [None] * len(entries)
    pkg_n = norm(pkg)
    primary_taken = False
    own_crates = [i for i, e in enumerate(entries)
                  if pkg.startswith("rust-") and e["name"].lower().endswith(".crate")
                  and (cn := crate_name(e["name"])) is not None and own_crate_name(pkg, upstream, cn)]

    def crate_at_upstream(i: int) -> bool:
        return norm(entries[i]["name"][:-len(".crate")]).endswith("-" + norm(upstream))

    if len(own_crates) > 1:  # several versions of the package's own crate: prefer the packaged one
        exact = [i for i in own_crates if crate_at_upstream(i)]
        own_primary = exact[0] if exact else own_crates[0]
    else:
        own_primary = own_crates[0] if own_crates else None
    for i, e in enumerate(entries):
        name = e["name"]
        k = kind_of(name)
        low = name.lower()
        if k == "signature":
            what = "keyring/public key" if ("keyring" in low or low.endswith(".gpg")) else "detached signature"
            suffix = "keyring" if "keyring" in low else low.rsplit(".", 1)[-1]
            out[i] = (k, "signature", f"filename suffix/marker '{suffix}' ({what})")
            continue
        if k == "crate":
            cn = crate_name(name)
            if i == own_primary:
                how = (f"; {len(own_crates)} versions of this crate listed, this one matches upstream version {upstream}"
                       if len(own_crates) > 1 and crate_at_upstream(i) else
                       f"; {len(own_crates)} versions of this crate listed, first taken" if len(own_crates) > 1 else "")
                out[i] = (k, "primary", f"{own_crate_name(pkg, upstream, cn)}{how}")
                primary_taken = True
            elif i in own_crates:
                out[i] = (k, "unknown", f"crate '{cn}' matches package name but another listed version "
                                        f"is primary (upstream version {upstream})")
            elif pkg.startswith("rust-"):
                out[i] = (k, "unknown", f"crate '{cn}' does not match package name {pkg}")
            else:
                out[i] = (k, "vendor_bundle", f"crate '{cn}' in non-rust package {pkg} (vendored crate)")
            continue
        vend = next((ev for rx, ev in VENDOR_PATTERNS if rx.search(name) and not rx.search(pkg)), None)
        if vend and k in ("tarball", "zip", "other"):
            out[i] = (k, "vendor_bundle", vend)
            continue
        if k == "patch" or (k in ("tarball", "zip") and "patch" not in pkg_n
                            and re.search(r"(^|[-_.])patch(es|set)?([-_.]|$)", low)):
            out[i] = (k, "patch_bundle", "patch file" if k == "patch"
                      else "archive filename contains 'patch(es)'")
            continue
    # Primary: among the remaining non-signature archives, rank by filename evidence
    # (contains upstream version, contains a package-name stem, no auxiliary marker such
    # as docs/data/tests), ties broken by sources-file position. A matching rust crate wins.
    stems = name_stems(pkg)
    ver_n = norm(upstream) if upstream else ""
    pkg_aux = bool(AUX_RE.search(pkg_n))
    facts = {}
    for i, e in enumerate(entries):
        if out[i] is not None:
            continue
        fn = norm(strip_ext(e["name"]))
        has_name = any(re.search(rf"(^|-){re.escape(s)}(-|$|\d)", fn) for s in stems) or any(
            len(s) >= 6 and s.replace("-", "") in fn.replace("-", "") for s in stems)
        has_ver = bool(ver_n) and re.search(rf"(^|-|[a-z]){re.escape(ver_n)}(-|$)", fn) is not None
        aux = (not pkg_aux) and AUX_RE.search(fn) is not None
        facts[i] = (has_name, has_ver, aux)

    def desc(i: int) -> str:
        has_name, has_ver, aux = facts[i]
        return (f"filename {'contains' if has_name else 'lacks'} package name"
                f", {'contains' if has_ver else 'lacks'} upstream version {upstream}"
                + (", has auxiliary marker (docs/data/tests/...)" if aux else ""))

    archives = [i for i in facts if kind_of(entries[i]["name"]) in ARCHIVE_KINDS]
    chosen = None
    if not primary_taken and archives:
        chosen = min(archives, key=lambda i: (not facts[i][1], not facts[i][0], facts[i][2], i))
    for i in sorted(facts):
        k = kind_of(entries[i]["name"])
        has_name, has_ver, aux = facts[i]
        if i == chosen:
            how = ("only non-signature, non-vendor archive" if len(archives) == 1 else
                   f"best-evidenced of {len(archives)} non-signature, non-vendor archives "
                   f"(rank: upstream version, package name, no auxiliary marker, then position)")
            out[i] = (k, "primary", f"{how}; dist-git sources position {i}; {desc(i)}")
        elif k in ARCHIVE_KINDS:
            role = "component" if (has_name or has_ver) else "unknown"
            out[i] = (k, role, f"additional archive besides primary (position {i}); {desc(i)}")
        else:
            what = ("checksum file" if re.search(r"\.(sha\d+|md5)(sum)?s?$", entries[i]["name"], re.I)
                    else "non-archive lookaside file")
            out[i] = (k, "unknown", f"{what} (position {i}); {desc(i)}")
    return out  # type: ignore[return-value]


def lookaside_url(pkg: str, e: dict) -> str:
    f = urllib.parse.quote(e["name"])
    if e["format"] == "legacy-md5":
        return f"{LOOKASIDE}/{urllib.parse.quote(pkg)}/{f}/{e['hex']}/{f}"
    return f"{LOOKASIDE}/{urllib.parse.quote(pkg)}/{f}/{e['algo']}/{e['hex']}/{f}"


# ---------------------------------------------------------------- records

def pin(build_answer: dict) -> tuple[dict | None, str | None, str | None, str]:
    """Return (build, repository, commit, failure-or-'')."""
    if "error" in build_answer:
        return None, None, None, "koji_request_error"
    if "fault" in build_answer:
        return None, None, None, "koji_fault"
    b = build_answer.get("build")
    if not b:
        return None, None, None, "koji_build_not_found"
    src = b.get("source") or ((b.get("extra") or {}).get("source") or {}).get("original_url")
    if not src:
        return b, None, None, "koji_no_source"
    m = SOURCE_RE.match(src)
    if not m:
        return b, None, None, "koji_source_unparsed"
    repo = m["repo"]
    ref = m["ref"]
    if not COMMIT_RE.match(ref):
        return b, repo, None, "koji_source_not_commit"
    return b, repo, ref, ""


def repo_name_of(repo: str) -> str:
    tail = repo.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def build_record(f: dict, kanswer: dict, collector: str, src_cache: dict) -> tuple[dict, list[str]]:
    name = f["name"]
    evr = f"{f['ver']}-{f['rel']}"
    version = evr if f["epoch"] in ("0", "", None) else f"{f['epoch']}:{evr}"
    nvr = f"{name}-{f['ver']}-{f['rel']}"
    rid = f"rpm:{name}@{version}"
    notes = [f"compose SRPM {f['location']} (sha256 {f['pkgid']})"]
    failures: list[str] = []
    build, repo, commit, fail = pin(kanswer)
    if build:
        notes.append(f"koji build {build.get('id')} {build.get('nvr')} source {build.get('source')}")
    packaging = {"kind": "git", "repository": None, "commit": None, "path": None,
                 "how_pinned": None, "metadata_pointer": None, "metadata_sha256": None}
    artifacts: list[dict] = []
    resolution = "partial"
    if fail:
        failures.append(fail)
        detail = kanswer.get("error") or (kanswer.get("fault") or {}).get("string") or ""
        notes.append(f"{fail}: Koji getBuild({nvr}) gave no commit" + (f" ({detail})" if detail else ""))
        if repo:
            packaging["repository"] = repo
    else:
        rname = repo_name_of(repo)
        packaging.update({
            "repository": f"{DISTGIT}/rpms/{rname}.git" if "fedoraproject.org" in repo else repo,
            "commit": commit, "path": None, "how_pinned": "koji-build-source",
        })
        if rname != name:
            notes.append(f"dist-git repository name {rname} differs from SRPM name {name}")
        s = src_cache[(rname, commit)]
        packaging["metadata_pointer"] = s.get("url")
        state = s["state"]
        if state == "present":
            packaging["metadata_sha256"] = s["body_sha256"]
            entries, bad = parse_sources(s["body"])
            for line in bad:
                failures.append("sources_line_unparsed")
                notes.append(f"sources_line_unparsed: {line!r}")
            if not entries and not bad:
                notes.append("sources file present but empty: no lookaside artifacts")
            roles = classify(name, f["ver"], entries)
            for n, (e, (kind, role, ev)) in enumerate(zip(entries, roles)):
                artifacts.append({
                    "id": f"{rid}#{n}", "name": e["name"], "url": lookaside_url(rname, e),
                    "size": None, "kind": kind, "role": role, "role_evidence": ev,
                    "declared": {e["algo"]: e["hex"]}, "hash_domain": "file_bytes",
                    "condition": None,
                })
            resolution = "partial" if bad else "resolved"
        elif state == "absent":
            notes.append("no dist-git sources file at commit (spec present): no external lookaside sources")
            resolution = "resolved"
        else:
            cat = "distgit_missing_commit" if state == "missing_commit" else "distgit_fetch_error"
            failures.append(cat)
            notes.append(f"{cat}: {s.get('detail')}")
    rec = {
        "id": rid, "family": "rpm", "source_package": name, "version": version,
        "upstream_version": f["ver"], "binary_packages": None,
        "packaging": packaging, "artifacts": artifacts,
        "live_vcs": False, "native": False, "resolution": resolution,
        "notes": notes, "collector": collector,
    }
    return rec, failures


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frame", type=Path, default=CACHE / "frame" / "fedora-primary.xml.zst")
    ap.add_argument("--out", type=Path, default=PROJECT / "evidence" / "scale" / "rpm.jsonl")
    ap.add_argument("--summary", type=Path, default=PROJECT / "evidence" / "scale" / "rpm-summary.json")
    ap.add_argument("--batch", type=int, default=100, help="getBuild calls per system.multicall")
    ap.add_argument("--koji-workers", type=int, default=3)
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--limit", type=int, default=0, help="debug: first N packages only")
    args = ap.parse_args()
    workers = min(args.workers, MAX_WORKERS)

    collector = f"scripts/scale/extract_rpm.py@{git_head()}"
    frame_sha = hashlib.sha256(args.frame.read_bytes()).hexdigest()
    frame = read_frame(args.frame)
    if args.limit:
        frame = frame[:args.limit]
    print(f"frame: {len(frame)} source packages", file=sys.stderr)

    nvrs = [f"{f['name']}-{f['ver']}-{f['rel']}" for f in frame]
    kanswers = koji_lookup_all(nvrs, args.batch, min(args.koji_workers, workers))

    todo = sorted({(repo_name_of(r), c) for b, r, c, fail in (pin(kanswers[n]) for n in nvrs)
                   if not fail})
    print(f"dist-git: {len(todo)} sources files to resolve", file=sys.stderr)
    src_cache: dict = {}
    done = 0
    lock = threading.Lock()

    def job(key):
        nonlocal done
        res = fetch_sources(*key)
        with lock:
            src_cache[key] = res
            done += 1
            if done % 1000 == 0:
                print(f"dist-git: {done}/{len(todo)}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(job, todo))

    records, fail_counter = [], Counter()
    for f, nvr in zip(frame, nvrs):
        rec, failures = build_record(f, kanswers[nvr], collector, src_cache)
        records.append(rec)
        fail_counter.update(set(failures))
    records.sort(key=lambda r: r["id"])
    ids = [r["id"] for r in records]
    dup = [i for i, c in Counter(ids).items() if c > 1]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")

    arts = [a for r in records for a in r["artifacts"]]
    states = Counter(src_cache[(repo_name_of(r["packaging"]["repository"]), r["packaging"]["commit"])]["state"]
                     for r in records if r["packaging"]["commit"])
    summary = {
        "schema": "pkgbridge-scale-summary/1",
        "family": "rpm",
        "collector": collector,
        "inputs": {"fedora_primary": str(args.frame.relative_to(CACHE)) if args.frame.is_relative_to(CACHE)
                   else str(args.frame), "fedora_primary_sha256": frame_sha,
                   "koji_hub": KOJI_HUB, "distgit": DISTGIT,
                   "raw_cache": "~/" + str(RAW.relative_to(Path.home()))},
        "records": len(records),
        "duplicate_ids": dup,
        "pinned_by_koji_commit": sum(1 for r in records if r["packaging"]["how_pinned"]),
        "sources_file_state": dict(sorted(states.items())),
        "records_with_artifacts": sum(1 for r in records if r["artifacts"]),
        "records_with_primary": sum(1 for r in records if any(a["role"] == "primary" for a in r["artifacts"])),
        "artifacts": len(arts),
        "artifacts_by_kind": dict(sorted(Counter(a["kind"] for a in arts).items())),
        "artifacts_by_role": dict(sorted(Counter(a["role"] for a in arts).items())),
        "hash_algorithms": dict(sorted(Counter(k for a in arts for k in a["declared"]).items())),
        "resolution": dict(sorted(Counter(r["resolution"] for r in records).items())),
        "failures_by_category": dict(sorted(fail_counter.items())),
        "binary_packages": "null in every record: the compose source repodata does not list binary RPM names",
        "role_rule_notes": [
            "signature = .asc/.sig/.sign/.gpg/.pgp suffix or a non-archive keyring file",
            "rust-<crate> packages: the .crate whose name matches the package is primary "
            "(the upstream-version one if several); other crates unknown (rust-*) or vendor_bundle",
            "vendor_bundle = archive filename contains vendor / crate(s) / -nm-prod|-nm-dev / node_modules",
            "primary otherwise = best-evidenced non-signature, non-vendor, non-patch archive in the "
            "dist-git sources file, ranked by: filename contains upstream version, contains a package-name "
            "stem, lacks auxiliary marker (docs/data/tests...), then file position (not spec Source index)",
            "other archives: component if the filename carries the package name or upstream version, else unknown",
            "lookaside URLs are the Fedora lookaside cache locations derived from the declared digest",
        ],
    }
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
