#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Checks tests/nix/read-opensta-guard-outputs.sh on a recorded ablation (the
# guard test's output on the unpatched OpenSTA 857316ff, x86_64-linux) and on
# copies of it with one change each: every refusal the reader has is reached,
# and the two readings it must accept are accepted. The guarded run is derived
# from the ablation (every check ok, PASS). No Nix, no sta; writes only under
# one mktemp directory.
#
# Usage: tests/nix/test-read-opensta-guard-outputs.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
read_outputs="$here/read-opensta-guard-outputs.sh"
work="$(mktemp -d "${TMPDIR:-/tmp}/adk-sta-guard-read.XXXXXX")"
trap 'rm -rf "$work"' EXIT
cd "$work" || exit 2

cat > ablation.out <<'OUT'
ok   buf_nogen: model written
ok   buf_nogen: model reads back
ok   buf_nogen: no table holds the allocator's fill
ok   buf_nogen: every table entry is finite and under 1 us
FAIL buf_nogen: fclk tables differ from the reached transitions
     1a2
     > CLK falling_edge cell_rise
     2a4,5
     > CLK falling_edge rise_transition
     > CLK rising_edge cell_fall
     3a7
     > CLK rising_edge fall_transition
ok   buf_nogen: fclk CLK rising_edge cell_rise is 0.10000,0.30000
ok   buf_nogen: Q carries exactly the reached transitions
ok   buf_nogen: Q CLK rising_edge cell_rise is 0.20000,0.40000
ok   buf_nogen: Q CLK rising_edge cell_fall is 0.22000,0.42000
ok   inv_nogen: model written
ok   inv_nogen: model reads back
ok   inv_nogen: no table holds the allocator's fill
ok   inv_nogen: every table entry is finite and under 1 us
FAIL inv_nogen: fclk tables differ from the reached transitions
     0a1
     > CLK falling_edge cell_fall
     1a3
     > CLK falling_edge fall_transition
     3a6
     > CLK rising_edge cell_rise
     4a8
     > CLK rising_edge rise_transition
ok   inv_nogen: fclk CLK rising_edge cell_fall is 0.09000,0.29000
ok   inv_nogen: Q carries exactly the reached transitions
ok   buf_gen: model written
ok   buf_gen: model reads back
FAIL buf_gen: a table holds the allocator's fill
FAIL buf_gen: a table entry is not finite or not under 1 us
FAIL buf_gen: fclk tables differ from the reached transitions
     1a2
     > CLK falling_edge cell_rise
     2a4,5
     > CLK falling_edge rise_transition
     > CLK rising_edge cell_fall
     3a7
     > CLK rising_edge fall_transition
     5a10
     > fclk falling_edge cell_rise
     6a12,13
     > fclk falling_edge rise_transition
     > fclk rising_edge cell_fall
     7a15
     > fclk rising_edge fall_transition
ok   buf_gen: fclk CLK rising_edge cell_rise is 0.10000,0.30000
ok   buf_gen: fclk fclk rising_edge cell_rise is 0.10000,0.30000
ok   buf_gen: Q carries exactly the reached transitions
FAILED: 5 check(s)
OUT
# The guarded run as the test prints it when every check passes: each failure
# in its success wording (the test's own pairs), the diffs dropped, PASS.
sed -E \
    -e 's/^FAIL ([a-z_]+): ([A-Za-z_]+) tables differ from the reached transitions$/ok   \1: \2 carries exactly the reached transitions/' \
    -e "s/^FAIL ([a-z_]+): a table holds the allocator's fill$/ok   \\1: no table holds the allocator's fill/" \
    -e 's/^FAIL ([a-z_]+): a table entry is not finite or not under 1 us$/ok   \1: every table entry is finite and under 1 us/' \
    -e '/^     /d' -e 's/^FAILED: .*/PASS/' ablation.out > guard.out
grep -q '^FAIL ' guard.out && { echo "FAIL the guarded fixture still holds a FAIL line"; exit 1; }

fails=0
# <label> <want rc> <want line prefix> <guarded out> <guarded rc> <ablation out> <ablation rc>
expect() {
    local got rc
    got="$(bash "$read_outputs" "$4" "$5" "$6" "$7")"
    rc=$?
    if [ "$rc" -eq "$2" ] && [ "${got#"$3"}" != "$got" ]; then
        echo "ok   $1"
    else
        echo "FAIL $1: rc $rc, '$got'"
        fails=$((fails + 1))
    fi
}
# one <sed arguments...> <file>: a copy of the ablation with that change.
one() { local out="${*: -1}"; sed "${@:1:$#-1}" ablation.out > "$out"; }

expect "recorded ablation accepted" 0 "accepted" guard.out 0 ablation.out 1
one -e "s/^FAIL buf_gen: a table holds the allocator's fill$/ok   buf_gen: no table holds the allocator's fill/" \
    -e 's/^FAIL buf_gen: a table entry is not finite or not under 1 us$/ok   buf_gen: every table entry is finite and under 1 us/' \
    allowed.out
expect "fill and finite may pass" 0 "accepted" guard.out 0 allowed.out 1
expect "guarded exit not 0" 1 "refused: the guarded run exits" guard.out 1 ablation.out 1
sed '$d' guard.out > g-nopass.out
expect "guarded without PASS" 1 "refused: the guarded run does not end in PASS" g-nopass.out 0 ablation.out 1
sed 's/^ok   buf_gen: model reads back$/FAIL buf_gen: model does not read back/' guard.out > g-fail.out
expect "guarded with a FAIL" 1 "refused: the guarded run reports a FAIL line" g-fail.out 0 ablation.out 1
printf 'PASS\n' > g-empty.out
expect "guarded with no check" 1 "refused: the guarded run reports no check" g-empty.out 0 ablation.out 1
expect "ablation exit 0" 1 "refused: the ablation exits 0" guard.out 0 ablation.out 0
expect "ablation exit 2" 1 "refused: the ablation exits 2" guard.out 0 ablation.out 2
one '$d' a-nocount.out
expect "ablation without its count" 1 "refused: the ablation does not end in FAILED" guard.out 0 a-nocount.out 1
one '/Q CLK rising_edge cell_fall/d' a-missing.out
expect "a check missing" 1 "refused: the ablation reports other checks" guard.out 0 a-missing.out 1
one 's/^FAIL inv_nogen: fclk tables differ from the reached transitions$/ok   inv_nogen: fclk carries exactly the reached transitions/' a-inv.out
expect "a table set not failing" 1 "refused: the ablation does not fail inv_nogen" guard.out 0 a-inv.out 1
one 's/^FAIL inv_nogen: fclk tables differ/ok   inv_nogen: fclk tables differ/' a-flip.out
expect "a status flipped, wording kept" 1 "refused: the ablation reports a line not in the test's wording" guard.out 0 a-flip.out 1
sed -E -e 's/^FAIL /ok   /' -e '/^     /d' -e 's/^FAILED: .*/PASS/' ablation.out > g-flip.out
expect "guarded with failure wording" 1 "refused: the guarded run reports a line not in the test's wording" g-flip.out 0 ablation.out 1
sed 's/^ok   buf_nogen: model written$/ok   buf_nogen: model was written/' guard.out > g-unknown.out
expect "guarded with an unknown line" 1 "refused: the guarded run reports a line not in the test's wording" g-unknown.out 0 ablation.out 1
one 's/^ok   buf_gen: model reads back$/FAIL buf_gen: model does not read back/' a-readback.out
expect "a readback failing" 1 "refused: the ablation fails a check it must pass: buf_gen: readback" guard.out 0 a-readback.out 1
one 's/^ok   inv_nogen: Q carries exactly the reached transitions$/FAIL inv_nogen: Q tables differ from the reached transitions/' a-q.out
expect "a Q check failing" 1 "refused: the ablation fails a check it must pass: inv_nogen: Q arcs" guard.out 0 a-q.out 1
one "s/^ok   buf_nogen: fclk CLK rising_edge cell_rise is 0.10000,0.30000$/FAIL buf_nogen: fclk CLK rising_edge cell_rise is '0.1', expected 0.10000,0.30000/" a-value.out
expect "a pinned value failing" 1 "refused: the ablation fails a check it must pass: buf_nogen: fclk CLK rising_edge cell_rise value" guard.out 0 a-value.out 1
bash "$read_outputs" guard.out 0 ablation.out > /dev/null 2>&1
if [ "$?" -eq 2 ]; then echo "ok   three arguments are a usage error"; else
    echo "FAIL three arguments are not a usage error"; fails=$((fails + 1)); fi

if [ "$fails" -eq 0 ]; then
    echo "PASS"
    exit 0
fi
echo "FAILED: $fails check(s)"
exit 1
