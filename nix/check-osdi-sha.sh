#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Checks that a directory holds exactly the OSDI files a sha256 list names, by
# their bytes: the files are not reproducible from their recipe by every
# compiler run, so their store path alone does not say which build it is.
# The list is nix/sg13g2-osdi.sha256's form, `<sha256>  <name>` as sha256sum
# prints it, one per line, sorted by name, '#' lines skipped. Refuses a list of
# another shape, a missing directory, and a directory whose *.osdi files are
# not exactly the list's names with the list's sha256 (the difference is
# printed, '<' listed, '>' found).
#
# Usage: nix/check-osdi-sha.sh <directory> <list>

set -uo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <directory> <list>" >&2; exit 2; }
dir="$1" list="$2"
refuse() {
    echo "osdi sha256: $*" >&2
    exit 1
}
[ -f "$list" ] || refuse "$list is missing"
want="$(grep -v '^#' "$list" || true)"
[ -n "$want" ] || refuse "$list names no file"
if grep -Evq '^[0-9a-f]{64}  [-.+_0-9A-Za-z]+\.osdi$' <<< "$want"; then
    refuse "$list has a line that is not '<sha256>  <name>.osdi'"
fi
[ "$want" = "$(LC_ALL=C sort -u -k2,2 <<< "$want")" ] || \
    refuse "$list is not sorted by name, or names a file twice"
[ -d "$dir" ] || refuse "$dir is not a directory"
got="$(cd "$dir" && shopt -s nullglob && files=(*.osdi) \
       && { [ ${#files[@]} -eq 0 ] || sha256sum -- "${files[@]}"; } | LC_ALL=C sort -k2,2)" || \
    refuse "could not read the files in $dir"
if [ "$got" != "$want" ]; then
    diff <(echo "$want") <(echo "$got") >&2 || true
    refuse "the *.osdi files in $dir are not the ones $list names"
fi
echo "osdi sha256: $dir holds the $(wc -l <<< "$want") files $list names"
