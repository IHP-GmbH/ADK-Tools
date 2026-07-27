#!/bin/bash
# Build the pinned OpenROAD image that provides read_3dbx / check_3dblox.
#
# Usage: [JOBS=N] ./openroad/build-image.sh [--force]
#
#   JOBS      parallel compile jobs (default: 8). OpenROAD's own Dockerfile
#             defaults to nproc, which would take the whole machine.
#   --force   rebuild even if the pinned tag already exists.
#
#   OPENROAD_SRC   existing OpenROAD checkout to build from. Must already be
#                  at the pinned commit; this script never moves someone
#                  else's checkout. Default: a managed clone under
#                  openroad/.openroad-src (git-ignored, ~3 GB).
#
# The image is NOT part of adk-tools:<tag>. It is a sidecar: OpenROAD stays
# in its own container so the distribution image does not carry a second
# multi-gigabyte toolchain. See README.md in this directory.
set -euo pipefail
cd "$(dirname "$0")"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

# shellcheck source=openroad.pin
source ./openroad.pin
JOBS="${JOBS:-8}"

if [ "$FORCE" -eq 0 ] && docker image inspect "$OPENROAD_3DBLOX_IMAGE" >/dev/null 2>&1; then
    echo "$OPENROAD_3DBLOX_IMAGE already exists; use --force to rebuild."
    exit 0
fi

SRC="${OPENROAD_SRC:-$PWD/.openroad-src}"
if [ -n "${OPENROAD_SRC:-}" ]; then
    # Caller-supplied checkout: verify, never mutate.
    head=$(git -C "$SRC" rev-parse HEAD)
    if [ "$head" != "$OPENROAD_COMMIT" ]; then
        echo "OPENROAD_SRC is at $head, pin requires $OPENROAD_COMMIT." >&2
        echo "Check it out yourself or unset OPENROAD_SRC to use the managed clone." >&2
        exit 1
    fi
else
    if [ ! -d "$SRC/.git" ]; then
        echo "Cloning OpenROAD into $SRC (this takes a while)."
        git clone --recurse-submodules "$OPENROAD_REPO" "$SRC"
    fi
    git -C "$SRC" fetch origin "$OPENROAD_COMMIT" 2>/dev/null \
        || git -C "$SRC" fetch origin
    git -C "$SRC" checkout --recurse-submodules --detach "$OPENROAD_COMMIT"
fi

# Loud check: the tag encodes the commit, so a mismatch here would ship an
# image whose name lies about what is inside it.
head=$(git -C "$SRC" rev-parse HEAD)
[ "$head" = "$OPENROAD_COMMIT" ] || {
    echo "checkout is at $head, expected $OPENROAD_COMMIT" >&2; exit 1; }

echo "Building $OPENROAD_3DBLOX_IMAGE from $SRC @ ${OPENROAD_COMMIT:0:8} (jobs=$JOBS)"
docker build \
    --build-arg "numThreads=$JOBS" \
    --build-arg "orVersion=${OPENROAD_COMMIT:0:8}" \
    -t "$OPENROAD_3DBLOX_IMAGE" \
    "$SRC"

echo
echo "Built $OPENROAD_3DBLOX_IMAGE. Next: ./openroad/verify-live.sh"
