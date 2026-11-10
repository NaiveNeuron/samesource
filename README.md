# SameSource: a byte-linked dataset of cross-distribution Linux package recipes

SameSource freezes Debian unstable, Fedora rawhide, Arch Linux (core, extra),
and Gentoo on 2026-11-05 and links their source packages by the digests of
the upstream archives each recipe declares. Two packages in one group build
from a byte-identical primary upstream archive. Every record points to its
pinned packaging state, so the recipes themselves can be retrieved.

## Quick start

The tables are attached to the
[v1.0.1 release](https://github.com/NaiveNeuron/samesource/releases/tag/v1.0.1):
`samesource.sqlite.zst` (SQLite), `samesource-parquet.tar` (Parquet), and
`samesource-jsonl.tar.zst` (the extracted records the tables are built from),
with `SHA256SUMS`.

```sh
gh release download v1.0.1 -R NaiveNeuron/samesource && shasum -a 256 -c SHA256SUMS
zstd -d samesource.sqlite.zst
sqlite3 samesource.sqlite "SELECT families, COUNT(*) FROM groups GROUP BY 1 ORDER BY 2 DESC;"
sqlite3 samesource.sqlite < examples/queries.sql
python3 scripts/scale/fetch_recipe.py --db samesource.sqlite 'pkgbuild:allegro4@4.4.3.1-5' 'rpm:allegro@4.4.3.1-21.fc45' --out recipes
```

DuckDB, pandas, or Polars can read the same tables from `parquet/`.

## Tables

| Table | Row | Columns |
|---|---|---|
| `records` | source-package revision | `id` (`family:name@version`), `family` (deb, rpm, pkgbuild, ebuild), `source_package`, `name_key` (lower-cased, Gentoo category dropped), `version`, `upstream_version`, `live_vcs`, `native`, `resolution`, `packaging_kind`, `packaging_repository`, `packaging_commit`, `packaging_path`, `how_pinned`, `metadata_pointer`, `metadata_sha256`, `binary_packages`, `notes` |
| `artifacts` | file a recipe downloads | `id` (`record#n`), `record_id`, `name`, `url`, `size`, `kind`, `role` (primary, component, vendor_bundle, patch_bundle, signature, packaging, unknown), `role_evidence`, `hash_domain` (file_bytes or vcs), `condition` |
| `digests` | digest of an artifact | `artifact_id`, `algorithm` (sha256, sha512, blake2b, md5), `digest`, `source` (declared, observed_download) |
| `links` | same-bytes assertion | `a`, `b` (artifact ids), `assertion` (same_file_bytes), `algorithms`, `evidence` (per algorithm: declared or observed on each side), `status`, `role_a`, `role_b` |
| `groups` | connected component of primary–primary links | `group_id`, `families`, `n_families`, `n_records` |
| `group_members` | membership | `group_id`, `record_id` |
| `tasks` | directional translation task | `task_id`, `group_id`, `split` (train, dev, test; per group), `tier`, `source_id`, `target_id`, `source_family`, `target_family`, `same_upstream_version_string` |
| `patch_declarations` | grouped record | `record_id`, `declared_patches`, `evidence` |

## How it was built

`scripts/scale/frame.py` pins the inputs (`evidence/scale/frame.json`); one
extractor per family writes JSONL records; `enrich_deb.py` and
`enrich_arch.py` stream archives to bridge SHA-256-only digests; `link.py`,
`names.py`, `patches.py`, `mismatch_causes.py`, and `tasks.py` derive the
analyses; `release.py` builds SQLite and Parquet. `evidence/scale/MANIFEST.json`
pins the SHA-256 of every large file.

## What a link does and does not mean

A link says two artifacts have the same bytes. A group says packages share a
primary upstream archive. Neither says the packages have the same complete
inputs, patches, dependencies, outputs, or behaviour, and a missing link does
not mean different software. VCS sources are recorded but not compared.
Patch counts are declarations, not observed source changes.

## Validation

`evidence/scale/audit-sheet.json` holds 180 sampled links (30 per distribution
pair). `audit-rater-a.json` and `audit-rater-b.json` are two labellings by LLM
agents, blind to each other; `audit-human-M.csv` is one author's labelling of
the same links, made without seeing the model labels.
`audit-adjudication.json` records the labels the author revised after the
comparison; the blind file is unchanged. `audit-summary.json` summarises all
of them (the author under `human_raters`, the revision under
`after_discussion`).

No rater found a link between different products or to a vendored bundle. The
two agents agreed on all 180 items: 173 links join the same project's primary
archive and the other seven join a part or derived build of it. The author
agreed with both agents on 175 items (kappa 0.43, low because one label
dominates) and judged 178 to be the same primary archive and 2 a component or
split. The 5 differences are sub-project and cross builds that the author
counted as the same source and the agents as a part (`autoconf-mode`,
`mingw-gsm`, `mingw-objfw`, `riscv64-elf-gdb`, `polly`). After the labels were
compared and the rubric discussed, the author revised 2 of them to component
or split (`riscv64-elf-gdb` and `polly`), which gives agreement on 177 items
and leaves 3 (`autoconf-mode`, `mingw-gsm`, `mingw-objfw`); the blind labels
are kept and remain the primary report. Whether such builds count is a rubric
boundary, so users who need one product per group should check names. There
is one human rater, so no agreement between humans is reported.

## Rights

The dataset contains metadata, digests, and pointers. Recipes and upstream
archives are not redistributed; they remain under their distributions' and
authors' terms. The scripts are under the MIT licence (`LICENSE`); the
derived data are under CC BY-NC 4.0 (`LICENSE-DATA`).
