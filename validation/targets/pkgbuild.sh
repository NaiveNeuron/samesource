#!/bin/bash
# Stages S and B for a PKGBUILD, run inside the pinned Arch image.
set -u
cd /work
record() { echo "$1=$2" >> /work/stages.env; }

echo "Server=${SNAPSHOT}" > /etc/pacman.d/mirrorlist
# pacman's seccomp download sandbox fails under amd64 emulation on arm64 hosts.
sed -i '/^\[options\]/a DisableSandbox' /etc/pacman.conf
{ pacman -Syyuu --noconfirm base-devel git unzip sudo &&
  useradd -m builder &&
  echo 'builder ALL=(ALL:ALL) NOPASSWD: ALL' > /etc/sudoers.d/builder &&
  # The host-mounted build directory can appear owned by another uid, which
  # makes git refuse the cloned source ("dubious ownership") at random.
  git config --system --add safe.directory '*'; } >/work/logs/setup.log 2>&1 ||
  { record SETUP fail; exit 0; }
pacman -Q | tr ' ' '=' | sort > /work/logs/toolchain.txt

mkdir -p build && cp recipe/PKGBUILD build/ && chown -R builder build
as_builder() { sudo -u builder env HOME=/home/builder bash -c "cd /work/build && $1"; }
if as_builder "makepkg --verifysource" >/work/logs/S.log 2>&1; then
  record S pass
else
  record S fail; exit 0
fi
if as_builder "makepkg --syncdeps --noconfirm --noprogressbar" >/work/logs/B.log 2>&1; then
  record B pass
else
  record B fail; exit 0
fi
mkdir -p out && find build -maxdepth 1 -name '*.pkg.tar.zst' ! -name '*-debug-*' -exec cp {} out/ \;
for p in out/*.pkg.tar.zst; do bsdtar -tf "$p"; done > /work/logs/manifest.txt
