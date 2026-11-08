#!/bin/bash
# Save one runtime image as the archive a release ships: docker save of the
# image ID, zstd-compressed, checked back, and listed in the directory's
# SHA256SUMS.
#
# Usage: ./save-image.sh <image ID> <directory>
#
#   <image ID>   sha256:<64 hex>, as run.sh takes it; a tag is refused (the
#                daemon is shared and a tag can be moved)
#   <directory>  an existing directory; the archive is written there as
#                adk-tools-runtime-<first 12 hex of the ID>.tar.zst, and its
#                line is added to <directory>/SHA256SUMS
#
# The archive is kept only if it reads back as the image, by
# ci/check_image_archive.py, which says what it checks. Two IDs come out of
# it, and the last line prints both: the config digest, what docker load gives
# back on a daemon that keys images by their config, and what run.sh and a
# release lock hold the image to; and the manifest digest, what a daemon on the
# containerd image store gives back.
#
# An archive of that name, or a SHA256SUMS line for it, already there is
# refused, never overwritten. The archive is written under a name of its own in
# the directory and linked into place only after the check, without replacing
# anything; if its line cannot be added it is removed again.
#
# docker save of a runtime image reads every layer, several GB.
set -euo pipefail

refuse() { echo "error: $*" >&2; exit 1; }
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

[ $# -eq 2 ] || refuse "usage: ./save-image.sh <image ID> <directory>"
ID="$1"
DIR="$2"
[[ "$ID" =~ ^sha256:([0-9a-f]{64})$ ]] || refuse "the image must be an ID, sha256:<64 hex> (got '$ID')"
HEX="${BASH_REMATCH[1]}"
[ -d "$DIR" ] || refuse "$DIR is not a directory"
command -v zstd >/dev/null || refuse "no zstd on PATH"

NAME="adk-tools-runtime-${HEX:0:12}.tar.zst"
OUT="$DIR/$NAME"
SUMS="$DIR/SHA256SUMS"

# sha256sum's own line, read as bin/adk-vscode reads it: 64 hex, a space, then
# a space or *, then the name as it is; a CRLF line and upper-case hex read as
# sha256sum -c reads them. The name goes through the environment, which awk
# takes as it is (-v would expand backslashes).
listed() {
    [ -f "$SUMS" ] || return 1
    NAME="$NAME" awk '{ sub(/\r$/, "") }
        substr($0, 65, 1) == " " && substr($0, 67) == ENVIRON["NAME"] &&
        (substr($0, 66, 1) == " " || substr($0, 66, 1) == "*") {found = 1; exit}
        END {exit !found}' "$SUMS"
}
# A last line with no newline would take the archive's line onto its own.
whole_lines() {
    [ ! -s "$SUMS" ] || [ -z "$(tail -c 1 -- "$SUMS")" ]
}
fresh() {
    [ ! -e "$OUT" ] && [ ! -L "$OUT" ] || refuse "$OUT is already there"
    ! listed || refuse "$SUMS already lists $NAME"
    whole_lines || refuse "$SUMS does not end in a newline; the archive's line would join its last"
}
fresh

SEEN="$(docker image inspect --format '{{.Id}}' "$ID" 2>/dev/null)" ||
    refuse "no image $ID on this docker daemon"
[ "$SEEN" = "$ID" ] || refuse "docker image inspect $ID gave another ID: '$SEEN'"

# A name of this run's own, so two runs never share a partial file.
PART="$(mktemp -p "$DIR" ".$NAME.XXXXXXXX.part")"
MOVED=0
cleanup() {
    rm -f -- "$PART"
    [ "$MOVED" -eq 0 ] || rm -f -- "$OUT"
}
trap cleanup EXIT

# The compressed bytes are hashed as they are written.
SUM="$(docker save "$ID" | zstd -q -T4 -3 -c | tee -- "$PART" | sha256sum)" ||
    refuse "docker save $ID or its compression failed"
SUM="${SUM%% *}"

# The check reads the archive back from disk, not from the pipe, and names
# each fault itself.
MANIFEST_DIGEST="$(python3 -I "$HERE/ci/check_image_archive.py" "$ID" "$PART")" ||
    refuse "$NAME is not kept: it does not read back as the image $ID (above)"

# Into place without replacing anything another run put there meanwhile, with
# the mode a file written here would have (mktemp makes it the owner's only).
# A hard link fails, atomically, when the name is taken, on any filesystem.
chmod -- "$(printf '%04o' $(( 0666 & ~0$(umask) )))" "$PART"
fresh
if ! LINK_ERROR="$(ln -T -- "$PART" "$OUT" 2>&1)"; then
    [ ! -e "$OUT" ] || refuse "$OUT is already there"
    refuse "could not put $NAME in place: $LINK_ERROR"
fi
MOVED=1
rm -f -- "$PART"
whole_lines && ! listed || refuse "$SUMS changed while $NAME was saved"
printf '%s  %s\n' "$SUM" "$NAME" >> "$SUMS" || refuse "could not add $NAME's line to $SUMS"
MOVED=0
trap - EXIT
echo "save-image: $OUT, $(stat -c %s -- "$OUT") bytes, sha256 $SUM, config $ID, manifest $MANIFEST_DIGEST"
