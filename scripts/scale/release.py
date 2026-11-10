#!/usr/bin/env python3
"""Build the queryable SameSource release: SQLite (+ Parquet when duckdb is present).

Tables
  records        one per source-package revision, with its pinned packaging state
  artifacts      files each recipe downloads, with role and role evidence
  digests        every digest: declared by the distribution, or observed by a
                 download whose declared SHA-256 (and size, where declared) matched
  links          same_file_bytes assertions with per-algorithm evidence
  groups, group_members   connected components over primary-primary links
  tasks          directional byte-matched translation tasks with splits
  patch_declarations      declared patch counts for grouped records
The JSONL under evidence/scale/ stays the source of truth.
Output: output/dataset/samesource.sqlite (and parquet/).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

PROJECT = Path(__file__).resolve().parents[2]
SCALE = PROJECT / "evidence" / "scale"
OUT = PROJECT / "output" / "dataset"
FAMILIES = ["deb", "rpm", "pkgbuild", "ebuild"]
TABLES = ("records", "artifacts", "digests", "links", "groups", "group_members", "tasks",
          "patch_declarations")

SCHEMA = """
CREATE TABLE records (id TEXT PRIMARY KEY, family TEXT, source_package TEXT, name_key TEXT,
  version TEXT, upstream_version TEXT, live_vcs INTEGER, native INTEGER, resolution TEXT,
  packaging_kind TEXT, packaging_repository TEXT, packaging_commit TEXT, packaging_path TEXT,
  how_pinned TEXT, metadata_pointer TEXT, metadata_sha256 TEXT, binary_packages TEXT,
  notes TEXT);
CREATE TABLE artifacts (id TEXT PRIMARY KEY, record_id TEXT REFERENCES records(id), name TEXT,
  url TEXT, size INTEGER, kind TEXT, role TEXT, role_evidence TEXT, hash_domain TEXT,
  condition TEXT);
CREATE TABLE digests (artifact_id TEXT REFERENCES artifacts(id), algorithm TEXT, digest TEXT,
  source TEXT CHECK (source IN ('declared', 'observed_download')));
CREATE TABLE links (a TEXT, b TEXT, assertion TEXT, algorithms TEXT, evidence TEXT,
  status TEXT, role_a TEXT, role_b TEXT);
CREATE TABLE groups (group_id TEXT PRIMARY KEY, families TEXT, n_families INTEGER,
  n_records INTEGER);
CREATE TABLE group_members (group_id TEXT, record_id TEXT);
CREATE TABLE tasks (task_id TEXT PRIMARY KEY, group_id TEXT, split TEXT, tier TEXT,
  source_id TEXT, target_id TEXT, source_family TEXT, target_family TEXT,
  same_upstream_version_string INTEGER);
CREATE TABLE patch_declarations (record_id TEXT PRIMARY KEY, declared_patches INTEGER,
  evidence TEXT);
CREATE INDEX digests_by_value ON digests(algorithm, digest);
CREATE INDEX digests_by_artifact ON digests(artifact_id);
CREATE INDEX artifacts_by_record ON artifacts(record_id);
CREATE INDEX links_a ON links(a);
CREATE INDEX links_b ON links(b);
CREATE INDEX members_by_record ON group_members(record_id);
CREATE INDEX records_by_name ON records(family, name_key);
"""


def group_id(records: list[str]) -> str:
    return hashlib.sha256("|".join(records).encode()).hexdigest()[:16]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    db_path = OUT / "samesource.sqlite"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    for fam in FAMILIES:
        for line in (SCALE / f"{fam}.jsonl").open():
            r = json.loads(line)
            p = r.get("packaging") or {}
            db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["id"], r["family"], r["source_package"],
                r["source_package"].split("/")[-1].lower(), r.get("version"),
                r.get("upstream_version"), int(bool(r.get("live_vcs"))), int(bool(r.get("native"))),
                r.get("resolution"), p.get("kind"), p.get("repository"), p.get("commit"),
                p.get("path"), p.get("how_pinned"), p.get("metadata_pointer"),
                p.get("metadata_sha256") or p.get("dsc_sha256"),
                json.dumps(r.get("binary_packages") or []), json.dumps(r.get("notes") or [])))
            for a in r["artifacts"]:
                db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    a["id"], r["id"], a.get("name"), a.get("url"), a.get("size"), a.get("kind"),
                    a.get("role"), a.get("role_evidence"), a.get("hash_domain"),
                    a.get("condition")))
                for alg, digest in (a.get("declared") or {}).items():
                    if digest:
                        db.execute("INSERT INTO digests VALUES (?,?,?,?)",
                                   (a["id"], alg, digest.lower(), "declared"))
    for name in ("deb-enrichment.jsonl", "pkgbuild-enrichment.jsonl"):
        path = SCALE / name
        if not path.exists():
            continue
        for line in path.open():
            row = json.loads(line)
            if row.get("declared_sha256_matches") and row.get("declared_size_matches"):
                for alg in ("sha512", "blake2b"):
                    db.execute("INSERT INTO digests VALUES (?,?,?,?)",
                               (row["artifact_id"], alg, row[alg], "observed_download"))
    for line in (SCALE / "links.jsonl").open():
        l = json.loads(line)
        db.execute("INSERT INTO links VALUES (?,?,?,?,?,?,?,?)", (
            l["a"], l["b"], l["assertion"], ",".join(l["algorithms"]),
            ",".join(l.get("evidence") or []), l["status"], *l["roles"]))
    for line in (SCALE / "groups.jsonl").open():
        g = json.loads(line)
        gid = group_id(g["records"])
        db.execute("INSERT INTO groups VALUES (?,?,?,?)",
                   (gid, ",".join(g["families"]), len(g["families"]), len(g["records"])))
        db.executemany("INSERT INTO group_members VALUES (?,?)", [(gid, r) for r in g["records"]])
    for line in (SCALE / "tasks.jsonl").open():
        t = json.loads(line)
        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)", (
            t["task_id"], group_id(t["group"].split("|")), t["split"], t["tier"],
            t["source"]["id"], t["target"]["id"], t["source"]["family"], t["target"]["family"],
            int(t["same_upstream_version_string"])))
    patches = json.loads((SCALE / "patches.json").read_text())["records"]
    db.executemany("INSERT INTO patch_declarations VALUES (?,?,?)",
                   [(rid, v["count"], v["evidence"]) for rid, v in patches.items()])
    db.commit()
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    db.close()
    try:
        import duckdb  # optional: Parquet export
    except ImportError:
        duckdb = None
    if duckdb is not None:
        pq = OUT / "parquet"
        pq.mkdir(exist_ok=True)
        con = duckdb.connect()
        con.execute(f"ATTACH '{db_path}' AS src (TYPE sqlite)")
        for table in TABLES:
            con.execute(f"COPY src.{table} TO '{pq / (table + '.parquet')}' (FORMAT parquet)")
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    summary = {"sqlite": db_path.name, "sha256": digest, "rows": counts, "parquet": duckdb is not None}
    (SCALE / "release-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
