#!/bin/bash
# Stage the sibling sources named in staging.pin into staging/, by commit.
#
# Usage: [STAGE_SIBLINGS=<dir>] ./stage-sources.sh [--check]
#
#   --check   resolve and report only; write nothing.
#
# Run by build.sh before the image build. Safe to run by hand.
#
# Why this exists at all is in staging.pin. What it guarantees is narrower and
# worth stating here, because it is the only reason staging is tolerable:
# every tree it writes is a fresh clone checked out at one commit, so it is
# exactly that commit, with no uncommitted edit, no untracked .venv and no
# __pycache__ able to ride along. A `cp -a` of a working tree has none of that
# property, and the image would then contain something with no name.
#
# The script refuses rather than guesses. An unset ref, a checkout that does
# not have the commit, a staged tree that comes out dirty: each stops the build
# here, before an hour of docker, and says which one.
set -euo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

[ -f staging.pin ] || { echo "stage: staging.pin not found" >&2; exit 1; }

# The directory staging.pin's paths are relative to: this checkout's parent,
# where the siblings sit in the usual layout, unless STAGE_SIBLINGS names it.
# A worktree kept elsewhere has no siblings beside it.
SIBLINGS="${STAGE_SIBLINGS:-$(cd .. && pwd)}"
case "$SIBLINGS" in
    /*) ;;
    *) echo "stage: STAGE_SIBLINGS must be an absolute path, not $SIBLINGS" >&2; exit 1 ;;
esac
STAGING=staging

# The staged entries are staging.pin's STAGE_<X>_PATH/_REF pairs, read through
# ci/pin-set.json, the one record of every pin here. A pin set that is not what
# its sources give stops the build: the image would then stage commits the
# tracked record does not name. Each tree lands under staging/ at its stage
# name, <x> in lower case, which is also what manifest.json calls it.
python3 -B ci/pin_set.py --check >/dev/null || {
    echo "stage: ci/pin-set.json does not match staging.pin; run python3 ci/pin_set.py" >&2
    exit 1
}

# ci/export-pins does the work, the same code the CI's exports run, and its
# docstring has the method. It resolves every entry before it writes one.
#
# Why a shallow fetch and not `git archive`: packaging-flow records the
# identity of every package it dispatches by running `git -C <package dir>
# rev-parse HEAD` on the directory find_spec resolves to. An archived tree has
# no .git, that command exits 128, and the identity is recorded as
# "unavailable: not a git checkout", which `packaging-flow verify` refuses at
# its full tier. Keeping .git costs 860 KB for the largest of these and makes
# the identity come out right by itself. The other half of the same
# requirement is the install: these go into the venv EDITABLE, since a regular
# install puts the package under site-packages, where there is no .git.
#
# A named extra commit (STAGE_<X>_EXTRA_REFS) is fetched and never checked out:
# HEAD stays the pin, and a consumer asking "does this tree carry the commit it
# declares" finds it. Deepening the clone would answer that by guessing how
# much history is enough; naming the commits keeps the contents a list.
EXPORT=(python3 -B ci/export-pins --pin-set ci/pin-set.json --source-root "$SIBLINGS" --stage-names)

if [ "$CHECK_ONLY" -eq 1 ]; then
    exec "${EXPORT[@]}" --check
fi

# staging/ is replaced wholesale: a leftover tree from a previous pin would
# otherwise sit in the build context, unnamed by the manifest, which is exactly
# the failure mode staging is supposed to avoid.
rm -rf "$STAGING"
"${EXPORT[@]}" --out "$STAGING"
