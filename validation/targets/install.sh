#!/bin/bash
# Stages I, K and F in a fresh container from the same pinned image.
# Env: TARGET, SNAPSHOT, SMOKE, BEHAVIOUR. /work/out holds built packages.
set -u
record() { echo "$1=$2" >> /work/stages.env; }

case "$TARGET" in
  deb)
    stamp=${SNAPSHOT%% *}
    printf 'deb http://snapshot.debian.org/archive/%s/%s %s main\n' \
      debian "$stamp" bullseye debian-security "$stamp" bullseye-security \
      debian "$stamp" bullseye-updates > /etc/apt/sources.list
    install() { apt-get -o Acquire::Check-Valid-Until=false update &&
      DEBIAN_FRONTEND=noninteractive apt-get install -y /work/out/*.deb; } ;;
  rpm)
    install() { dnf install -y --disablerepo='*' --enablerepo=fedora /work/out/*.rpm; } ;;
  pkgbuild)
    echo "Server=${SNAPSHOT}" > /etc/pacman.d/mirrorlist
    sed -i '/^\[options\]/a DisableSandbox' /etc/pacman.conf
    install() { pacman -Syy --noconfirm && pacman -U --noconfirm /work/out/*.pkg.tar.zst; } ;;
esac

if install >/work/logs/I.log 2>&1; then record I pass; else record I fail; exit 0; fi
if bash -c "$SMOKE" >/work/logs/K.log 2>&1; then record K pass; else record K fail; fi
if bash -c "$BEHAVIOUR" >/work/logs/F.log 2>&1; then record F pass; else record F fail; fi
