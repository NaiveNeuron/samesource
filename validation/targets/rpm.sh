#!/bin/bash
# Stages S and B for an RPM spec, run inside the pinned Fedora image.
# /work/recipe holds the stored spec; /work/sources_reference is dist-git `sources`.
set -u
cd /work
record() { echo "$1=$2" >> /work/stages.env; }

repos=(--disablerepo='*' --enablerepo=fedora)
dnf install -y "${repos[@]}" rpm-build rpmdevtools 'dnf-command(builddep)' >/work/logs/setup.log 2>&1 ||
  { record SETUP fail; exit 0; }
spec=$(ls recipe/*.spec)
dnf builddep -y "${repos[@]}" "$spec" >>/work/logs/setup.log 2>&1 || { record SETUP fail; exit 0; }
rpm -qa --qf '%{NAME}=%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n' | sort > /work/logs/toolchain.txt

# Build on the container's own filesystem: unpacking onto the bind-mounted
# host directory fails on macOS hosts (tar cannot restore xattrs there).
top=/root/rpmbuild
mkdir -p "$top"/{SOURCES,SPECS}
cp "$spec" "$top/SPECS/"
# S: every source the spec names must be recorded in dist-git `sources` and is
# fetched from the Fedora lookaside cache by that hash (upstream URLs can serve
# re-compressed bytes).
s_ok=1
urls=$(spectool -l -a "$spec" 2>>/work/logs/S.log | awk '/^Source[0-9]*:/ {print $2}')
[[ -n $urls ]] || { echo "spectool listed no sources" >>/work/logs/S.log; s_ok=0; }
for url in $urls; do
  name=${url##*/}
  hash=$(sed -n "s/^SHA512 (${name}) = //p" /work/sources_reference)
  if [[ -z $hash ]]; then echo "not recorded: $name" >>/work/logs/S.log; s_ok=0; continue; fi
  curl -fsSL -o "$top/SOURCES/$name" \
    "https://src.fedoraproject.org/repo/pkgs/rpms/${PKG}/${name}/sha512/${hash}/${name}" \
    >>/work/logs/S.log 2>&1 &&
    echo "$hash  $top/SOURCES/$name" | sha512sum -c >>/work/logs/S.log 2>&1 || s_ok=0
done
if (( s_ok )); then
  record S pass
else
  record S fail; exit 0
fi
if rpmbuild --define "_topdir $top" -bb "$top/SPECS/$(basename "$spec")" >/work/logs/B.log 2>&1; then
  record B pass
else
  record B fail; exit 0
fi
mkdir -p out && find "$top/RPMS" -name '*.rpm' ! -name '*debug*' -exec cp {} out/ \;
for p in out/*.rpm; do rpm -qlp "$p"; done > /work/logs/manifest.txt
