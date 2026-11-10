#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Reads two runs of tests/nix/test-opensta-timing-model-guard.sh, one on the
# guarded sta and one on the unpatched sta (its ablation), and accepts only
# when they mean what checks.opensta says they mean:
#
#   guarded    exit 0, last line PASS, no FAIL line.
#   ablation   exit 1, last line "FAILED: <n> check(s)", the same checks run
#              as on the guarded sta (a check missing or added is refused),
#              the three forwarded-clock table-set checks (buf_nogen,
#              inv_nogen, buf_gen) among the failures, and no other failure
#              but a case's allocator-fill or finite-value check. Those two may
#              or may not fail, since what an unset table holds depends on the
#              heap. Every other check (model written, read back, Q, the pinned
#              values) must pass on both.
#
# The test words a check differently when it passes and when it fails, so
# each line is reduced to its check (case and what is checked) by the test's
# exact message pairs before the two runs are compared. A line whose wording
# is not one the test prints with that status is refused.
#
# Exit 0 when accepted, 1 with the first reason when refused, 2 on usage.
#
# Usage: tests/nix/read-opensta-guard-outputs.sh <guarded out> <guarded rc> <ablation out> <ablation rc>

set -uo pipefail
[ "$#" -eq 4 ] || {
    echo "usage: $0 <guarded out> <guarded rc> <ablation out> <ablation rc>" >&2
    exit 2
}
g="$1" grc="$2" a="$3" arc="$4"
refuse() {
    echo "refused: $*"
    exit 1
}
# One line per reported check: "ok <check>", "FAIL <check>", or "<status> UNKNOWN <line>" for a
# line whose wording is not one the test gives that status. The pairs are the test's own.
checks() {
    awk -v q="'" '
        /^ok   / { s = "ok" } /^FAIL / { s = "FAIL" }
        !/^(ok   |FAIL )/ { next }
        { t = substr($0, 6)
          if (!match(t, /^[a-z_]+: /)) { print s, "UNKNOWN", t; next }
          c = substr(t, 1, RLENGTH - 2); r = substr(t, RLENGTH + 1); k = ""; w = ""
          if (r == "model written") { k = "model"; w = "ok" }
          else if (r == "no model written (see below)") { k = "model"; w = "FAIL" }
          else if (r == "model reads back") { k = "readback"; w = "ok" }
          else if (r == "model does not read back") { k = "readback"; w = "FAIL" }
          else if (r == "no table holds the allocator" q "s fill") { k = "fill"; w = "ok" }
          else if (r == "a table holds the allocator" q "s fill") { k = "fill"; w = "FAIL" }
          else if (r == "every table entry is finite and under 1 us") { k = "finite"; w = "ok" }
          else if (r == "a table entry is not finite or not under 1 us") { k = "finite"; w = "FAIL" }
          else if (r ~ /^[A-Za-z_]+ carries exactly the reached transitions$/) {
              k = r; sub(/ carries exactly the reached transitions$/, " arcs", k); w = "ok" }
          else if (r ~ /^[A-Za-z_]+ tables differ from the reached transitions$/) {
              k = r; sub(/ tables differ from the reached transitions$/, " arcs", k); w = "FAIL" }
          else if (r ~ ("^[A-Za-z_]+ [A-Za-z_]+ [a-z_]+ [a-z_]+ is " q "[^" q "]*" q ", expected [^ ]+$")) {
              k = r; sub(" is " q "[^" q "]*" q ", expected ", " value ", k); w = "FAIL" }
          else if (r ~ /^[A-Za-z_]+ [A-Za-z_]+ [a-z_]+ [a-z_]+ is [^ ]+$/) {
              k = r; sub(/ is /, " value ", k); w = "ok" }
          if (k == "" || s != w) print s, "UNKNOWN", t; else print s, c ": " k }
    ' "$1" | LC_ALL=C sort
}
cases="buf_nogen inv_nogen buf_gen"

[ "$grc" = 0 ] || refuse "the guarded run exits $grc"
[ "$(tail -n 1 "$g")" = PASS ] || refuse "the guarded run does not end in PASS"
gc="$(checks "$g")"
[ -n "$gc" ] || refuse "the guarded run reports no check"
unknown="$(grep ' UNKNOWN ' <<< "$gc")"
[ -z "$unknown" ] || refuse "the guarded run reports a line not in the test's wording for its status: $(head -n 1 <<< "$unknown")"
grep -q '^FAIL ' <<< "$gc" && refuse "the guarded run reports a FAIL line"

[ "$arc" = 1 ] || refuse "the ablation exits $arc, expected 1"
tail -n 1 "$a" | grep -q -x -E 'FAILED: [0-9]+ check\(s\)' || \
    refuse "the ablation does not end in FAILED: <n> check(s)"
ac="$(checks "$a")"
unknown="$(grep ' UNKNOWN ' <<< "$ac")"
[ -z "$unknown" ] || refuse "the ablation reports a line not in the test's wording for its status: $(head -n 1 <<< "$unknown")"
[ "$(cut -d' ' -f2- <<< "$gc" | LC_ALL=C sort)" = "$(cut -d' ' -f2- <<< "$ac" | LC_ALL=C sort)" ] || \
    refuse "the ablation reports other checks than the guarded run"
af="$(grep '^FAIL ' <<< "$ac" | cut -d' ' -f2-)"
for c in $cases; do
    grep -q -x -F "$c: fclk arcs" <<< "$af" || refuse "the ablation does not fail $c's fclk table set"
done
extra="$(grep -v -x -E "(buf_nogen|inv_nogen|buf_gen): (fclk arcs|fill|finite)" <<< "$af")"
[ -z "$extra" ] || refuse "the ablation fails a check it must pass: $(head -n 1 <<< "$extra")"
echo "accepted: guarded PASS on $(grep -c . <<< "$gc") checks; ablation fails $(grep -c . <<< "$af"), the three fclk table sets among them"
exit 0
