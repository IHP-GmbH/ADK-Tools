#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The image's technology files against a packaging-flow tree, without a build:
# ci/check_tech_files.py, this checkout's copy, runs in a built verify image
# with the tree mounted over the staged one, with the same invocation as the
# verify stage (the checker's defaults). A packaging-flow pin move that names a
# layer sheet, its .lyt or a stackup the image does not carry fails here in
# seconds, where the build would fail at that verify step an hour in.
#
# The image is the one named, so this does not see a change to the recipe: a
# Dockerfile that drops a file still fails only in its own build. The tree is
# imported with the image's venv, so a tree that needs a dependency the image
# lacks fails on the import, and says so, before any file is checked.
#
# The image is named by its ID, never by a tag: the daemon is shared and a tag
# can be moved (run.sh, openroad/check-3dblox.sh). Any docker run takes a host
# slot; run this through ci-runner's hostslot.
#
# Usage: check_image_tech_files.sh VERIFY_IMAGE_ID [PACKAGING_FLOW_TREE]
#        (the tree defaults to this checkout's staging/packaging-flow)

set -euo pipefail

fail() {
    echo "image tech files: $*" >&2
    exit 1
}

here="$(cd "$(dirname "$0")" && pwd)"
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || fail "usage: $0 VERIFY_IMAGE_ID [PACKAGING_FLOW_TREE]"
image="$1"
[[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    fail "the image must be an ID, sha256:<64 hex> (got '$image'); a tag is not taken"
tree="$(realpath "${2:-$here/../staging/packaging-flow}")"
[ -f "$tree/src/packaging_flow/technologies.py" ] || fail "$tree is not a packaging-flow tree"
[ "$(git -C "$tree" rev-parse --show-toplevel 2>/dev/null)" = "$tree" ] || \
    fail "$tree is not the top of a git checkout, so its commit cannot be said"
at="$(git -C "$tree" describe --always --dirty --abbrev=40)"

echo "image $image"
echo "packaging-flow $tree at $at"
exec docker run --rm --pull never --network none --cpus 1 --memory 1g --entrypoint "" \
    -v "$tree:/opt/adk-tools/staging/packaging-flow:ro" \
    -v "$here/check_tech_files.py:/opt/adk-tools/ci/check_tech_files.py:ro" \
    "$image" \
    /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_tech_files.py
