#!/bin/bash
# Replay corpus reconstruction and the version-alignment audit in a fresh,
# digest-pinned Python container that sees only the artifact (read-only).
# Writes evidence/clean-replay.json; compares with the recorded evidence.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
image=python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
out=$(mktemp -d)
docker run --rm --network bridge -v "$root:/artifact:ro" -v "$out:/out" "$image" bash -c '
  set -euo pipefail
  apt-get update -qq >/dev/null && apt-get install -y -qq git >/dev/null
  cd /artifact
  python3 scripts/fetch_corpus.py --out /out/corpus --record /out/reconstruction.json >/out/fetch.log
  # snapshot.debian.org often answers 504; its first-seen dates only feed the
  # publication-span field, so the recorded ones are reused here.
  python3 scripts/version_alignment.py --corpus /out/corpus --output /out/version-alignment.json \
    --debian-dates-from evidence/version-alignment.json >/dev/null
  python3 --version > /out/python-version.txt
'
python3 - "$root" "$out" "$image" <<'EOF'
import json, sys
from pathlib import Path
root, out, image = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
rec = json.loads((out / "reconstruction.json").read_text())
ref = json.loads((root / "evidence" / "reconstruction.json").read_text())
va = json.loads((out / "version-alignment.json").read_text())["summary"]
va_ref = json.loads((root / "evidence" / "version-alignment.json").read_text())["summary"]
result = {
    "schema": "pkgbridge-clean-replay/1",
    "image": image,
    "python": (out / "python-version.txt").read_text().strip(),
    "reconstruction_counts": rec["counts"],
    "rebuilt_tree_matches_recorded": rec["rebuilt_tree"] == ref["rebuilt_tree"],
    "alignment_summary_matches_recorded": va == va_ref,
    "seconds": rec["seconds"],
}
(root / "evidence" / "clean-replay.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
EOF
