#!/bin/bash
# Stage the die view records die-views.sha256 lists into die-views/.
#
# Usage: STAGE_DIE_VIEWS_DIR=<absolute dir> ./stage-die-views.sh [--check]
#
#   --check   check the source only; write nothing.
#
# Run by build.sh after stage-sources.sh. Safe to run by hand.
#
# The records are not in git and not in a repository a sibling checkout could
# hold: they are the output of a die run. So the pin is the listing, not a
# commit. die-views.sha256 names every file of every record by its sha256, and
# staging.pin's STAGE_DIE_VIEWS_SHA256 names the listing by its own, so the
# recipe commit pins every byte. Where the records sit on this host is a local
# fact, not part of the recipe: STAGE_DIE_VIEWS_DIR names the directory the
# listing's paths (records/<layout sha256>/<file>) are relative to, from the
# environment only. Nothing defaults it; an unset, relative or absent directory
# stops the build here.
#
# The source is only read. The check runs on it first, then each listed file is
# copied into die-views/<layout sha256>/<file> here, and the copy is checked
# again: the image gets what the copy holds, so the copy is what has to be
# right. A symlink in either refuses, as does a missing, extra or changed file,
# or a directory whose record is bound to another layout.
set -euo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

fail() { echo "stage die views: $*" >&2; exit 1; }

LISTING=die-views.sha256
DEST=die-views
[ -f staging.pin ] || fail "staging.pin not found"
PIN="$(bash -euo pipefail -c '. ./staging.pin >/dev/null 2>&1; printf "%s" "${STAGE_DIE_VIEWS_SHA256:-}"')"
[[ "$PIN" =~ ^[0-9a-f]{64}$ ]] || \
    fail "staging.pin's STAGE_DIE_VIEWS_SHA256 ('$PIN') is not a sha256"
[ -f "$LISTING" ] || fail "$LISTING not found"

SRC="${STAGE_DIE_VIEWS_DIR-}"
[ -n "$SRC" ] || fail "STAGE_DIE_VIEWS_DIR is not set.

  It names the directory holding records/, the one the paths in $LISTING are
  relative to. It is a fact about this host, so it comes from the environment
  and never from a default."
case "$SRC" in
    /*) ;;
    *) fail "STAGE_DIE_VIEWS_DIR must be an absolute path, not $SRC" ;;
esac
[ -d "$SRC" ] && [ ! -L "$SRC" ] || fail "STAGE_DIE_VIEWS_DIR $SRC is not a directory"

check() {
    python3 -B ci/check_die_views.py --listing "$LISTING" --expect-sha256 "$PIN" --root "$1"
}
check "$SRC/records" || fail "the records in $SRC are not the ones $LISTING pins"

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "stage die views: --check only, nothing written"
    exit 0
fi

# die-views/ is replaced wholesale, for the reason staging/ is: a file left from
# an earlier listing would sit in the build context, named by nothing.
rm -rf "$DEST"
mkdir -p "$DEST"
while read -r _ path; do
    rel="${path#records/}"
    install -D -m 0644 -T "$SRC/$path" "$DEST/$rel"
done < "$LISTING"
check "$DEST" || fail "the copy in $DEST is not what $LISTING pins"
