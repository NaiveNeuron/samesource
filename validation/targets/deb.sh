#!/bin/bash
# Stages S and B for a Debian source package, run inside the pinned image.
# /work/recipe holds the stored bundle; /work/sources holds snapshot bytes.
set -u
cd /work
record() { echo "$1=$2" >> /work/stages.env; }

stamp=${SNAPSHOT%% *}
cat > /etc/apt/sources.list <<EOF
deb http://snapshot.debian.org/archive/debian/$stamp bullseye main
deb http://snapshot.debian.org/archive/debian-security/$stamp bullseye-security main
deb http://snapshot.debian.org/archive/debian/$stamp bullseye-updates main
EOF
apt-get -o Acquire::Check-Valid-Until=false update >/work/logs/setup.log 2>&1 &&
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential debhelper zlib1g-dev quilt dpkg-dev >>/work/logs/setup.log 2>&1 ||
  { record SETUP fail; exit 0; }
dpkg-query -W -f='${Package}=${Version}\n' | sort > /work/logs/toolchain.txt

mkdir -p build && cp recipe/*.dsc sources/* build/
if (cd build && dpkg-source -x ./*.dsc src) >/work/logs/S.log 2>&1; then
  record S pass
else
  record S fail; exit 0
fi
for f in control rules; do
  if cmp -s "recipe/$f" "build/src/debian/$f"; then record "MATCH_$f" identical
  else record "MATCH_$f" differs; fi
  cp "recipe/$f" "build/src/debian/$f"
done
chmod +x build/src/debian/rules
if (cd build/src && dpkg-buildpackage -us -uc -b) >/work/logs/B.log 2>&1; then
  record B pass
else
  record B fail; exit 0
fi
mkdir -p out && cp build/*.deb out/ && rm -f out/*dbgsym*
for p in out/*.deb; do dpkg-deb -c "$p"; done | awk '{print $6}' > /work/logs/manifest.txt
