# PkgBridge seed: provenance and checks (part of SameSource)

This artifact accompanies the MSR 2027 Data and Tool Showcase paper
*A Provenance-Traced, Reconstructible Corpus of Cross-Distribution Package
Recipes*. It rebuilds the 130-program, five-ecosystem recipe corpus of
PkgBridge (commit `1a8ba8a2`, from an earlier thesis) from the distributions'
own public histories, audits version alignment, and runs calibrated native
build checks. It contains no complete recipe files (the identity audit quotes
short excerpts) and no PkgBridge code: every recipe is downloaded from its
distribution and verified by hash.

## Requirements

- Python 3.10 or later (standard library only) and Git, for the corpus.
- Docker with `linux/amd64` emulation for the Arch controls; the Fedora
  control runs on the host architecture. About 5 GB of image and cache space.
- Network access to src.fedoraproject.org, gitlab.archlinux.org,
  aur.archlinux.org, raw.githubusercontent.com, snapshot.debian.org, and
  archive.archlinux.org.

## Contents

| Path | Purpose |
|---|---|
| `evidence/provenance.json` | One record per program and ecosystem: stored path, git blob id or Debian hashes, upstream repository, commit or source version, upstream path, date, search log |
| `scripts/fetch_corpus.py` | Rebuild `dataset/<family>/<program>/` from the manifest; aborts on any hash mismatch |
| `scripts/version_alignment.py` | Literal upstream versions per recipe, pairwise and per-task agreement |
| `scripts/recover_provenance.py` | Regenerate the manifest from a PkgBridge checkout (needs that checkout) |
| `validation/controls.json` | Frozen calibration controls and expected stage outcomes |
| `validation/run_controls.py`, `validation/targets/*.sh` | Run the controls in digest-pinned containers |
| `evidence/version-alignment.json`, `evidence/release-validation*.json` | Recorded outputs of the two audits |

## Quick start

```sh
python3 scripts/fetch_corpus.py --out corpus           # ~1 minute, 650 bundles
python3 scripts/version_alignment.py --corpus corpus   # prints the summary
python3 validation/run_controls.py --source-root corpus --only deb:reference
```

`fetch_corpus.py` prints `{"verified": 650}` when every file matches.
`version_alignment.py` reports, among other values, `all_five_agree: 3` of
`all_five_resolved: 88` and `configured_tasks.different: 72`. A single
control takes 3 to 15 minutes; all twelve take a few hours under emulation.
Each control writes `stages.env` and logs under
`~/.cache/pkgbridge/validation/<target>-<program>-<variant>/`.

## Stage meanings

S source verified against a recorded hash; B native build exits 0; M built
package contains the expected paths; I installs in a fresh container from the
same image; K the PkgBridge smoke command exits 0 (exit status only; the thesis
also compared its output with the distribution package's); F a prespecified
behaviour check exits 0. Later stages are `not_evaluated` after S or B fails.

## Interpretation limits

A byte-identical match locates published bytes; it does not show where the
collector obtained them, that the recipe builds, or that two ecosystems
package the same software. Version agreement is textual. The three controls
show that the stages separate known-good and known-bad packages for those
programs only.

## Rights

Recipes are fetched from the distributions and remain under their terms. The
scripts in this artifact are under the MIT licence (`LICENSE`) and the derived
data under CC BY-NC 4.0 (`LICENSE-DATA`). The 130-program selection originates from PkgBridge
(https://github.com/Aethelsirius/pkgbridge) and the thesis of M. D. Szasz
(Comenius University in Bratislava, 2026).
