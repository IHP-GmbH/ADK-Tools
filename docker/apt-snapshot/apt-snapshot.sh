#!/bin/sh
# apt-get install from the Ubuntu snapshot service, at one fixed time.
#
# Usage: apt-snapshot.sh <YYYYMMDDTHHMMSSZ> [apt-get install options] <package>...
#
# Every apt step of the Dockerfile runs through this, bind-mounted for that
# RUN alone, so nothing of it is left in a layer. The package index is the
# archive as it stood at that time (https://snapshot.ubuntu.com/ubuntu/<time>/),
# so the same Dockerfile installs the same versions on any day. The only source
# apt reads is the one written here: the image's own sources list is set aside
# for the call, and left as it was.
#
# The service answers https only, and the base image has no CA bundle, so the
# one root its chain ends at (ISRG Root X1, next to this file) is handed to apt
# for the call. A fetch that fails stops the build here, with the time it asked
# for. There is no fallback to the live archive: a fallback would install
# whatever the archive holds that day under a key that says otherwise.
#
# The service is slow and answers 503 under load, so the downloaded packages are
# kept across builds when the RUN mounts a BuildKit cache at $cache (the
# Dockerfile does): one directory per snapshot time, so a rerun or a new tools
# key fetches only what is missing. It is not /var/cache/apt/archives, which the
# base image's docker-clean hook empties after every install, and a cache mount
# is never part of a layer. apt checks every package against the snapshot's
# index, so a cached file can only be the one the index names.
#
# A snapshot never changes, so its index is kept the same way, in the same
# directory: the first update that brings every suite's index writes a marker
# (the hash of the sources written below), and every later call that finds the
# marker skips the update. The first fetch tries up to six times, a minute
# apart, before it refuses. The RUNs share the cache with sharing=locked, so one
# apt step at a time talks to the service, and /var/lib/apt/lists in the image
# stays empty.
set -eu

here="$(dirname "$0")"
stamp="${1:-}"
case "$stamp" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
    *) echo "apt-snapshot: the snapshot time is '$stamp', not YYYYMMDDTHHMMSSZ (APT_SNAPSHOT)" >&2
       exit 1 ;;
esac
shift
[ "$#" -gt 0 ] || { echo "apt-snapshot: no package named" >&2; exit 1; }

url="https://snapshot.ubuntu.com/ubuntu/$stamp/"
cache=/var/cache/apt-snapshot
archives=/var/cache/apt/archives
lists=/var/lib/apt/lists
marker=
if [ -d "$cache" ]; then
    archives="$cache/$stamp"
    lists="$cache/$stamp/lists"
    marker="$lists/complete"
    mkdir -p "$archives/partial" "$lists/partial"
fi
suites="noble noble-updates noble-security"
parts="$(mktemp -d)"
cat > "$parts/snapshot.sources" <<EOF
Types: deb
URIs: $url
Suites: $suites
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF

apt_get() {
    apt-get -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts="$parts" \
        -o Acquire::https::CaInfo="$here/isrg-root-x1.pem" -o Acquire::Retries=5 \
        -o Dir::Cache::archives="$archives" -o APT::Keep-Downloaded-Packages=true \
        -o Dir::State::Lists="$lists" "$@"
}

refuse() {
    echo "apt-snapshot: $1 from the snapshot at $stamp ($url)." >&2
    echo "apt-snapshot: the build stops here; it does not fall back to the live archive." >&2
    [ -z "$marker" ] || rm -f "$marker"
    rm -rf /var/lib/apt/lists/* "$parts"
    exit 1
}

sources="$(sha256sum < "$parts/snapshot.sources" | cut -d' ' -f1)"
if [ -n "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$sources" ]; then
    echo "apt-snapshot: the index for $stamp is complete in the cache; update skipped"
else
    tries=6
    n=1
    until apt_get -o APT::Update::Error-Mode=any update; do
        [ "$n" -lt "$tries" ] || refuse "could not fetch the package index ($tries tries)"
        echo "apt-snapshot: index fetch $n of $tries failed; next try in 60 s" >&2
        n=$((n + 1))
        sleep 60
    done
fi
# Each suite's index has to be there, whatever apt's exit status said.
for suite in $suites; do
    ls "$lists"/*_dists_"${suite}"_InRelease > /dev/null 2>&1 \
        || refuse "no $suite index arrived"
done
[ -z "$marker" ] || echo "$sources" > "$marker"
apt_get install -y "$@" || refuse "could not install $*"
if [ "$archives" != /var/cache/apt/archives ]; then
    echo "apt-snapshot: $(ls "$archives" | grep -c '\.deb$') packages kept in the cache for $stamp"
fi
rm -rf /var/lib/apt/lists/* "$parts"
