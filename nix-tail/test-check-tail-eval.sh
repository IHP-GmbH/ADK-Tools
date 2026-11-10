#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for nix-tail/check-tail-eval.sh, without Nix. The check runs against
# two stand-in checkouts (one commit each) and a stand-in `nix` on PATH that
# records every call and prints the fixture nix-tail/test_tail_eval.py writes:
# the shells and packages of whichever checkout the installable names, and the
# requested derivations out of one show, with the store files the fixture's
# tail-wrappers names under a stand-in store (ADK_NIX_STORE_DIR). What the
# reader decides is tested in test_tail_eval.py; here, what the check runs,
# what it keeps, what it refuses before running anything, and that a reader's
# refusal is the check's.
#
# Needs bash, git, python3 and coreutils. Writes only under one mktemp
# directory.
#
# Usage: nix-tail/test-check-tail-eval.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
check="$here/check-tail-eval.sh"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-tail-eval-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
work="$(realpath "$work")"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
export PYTHONDONTWRITEBYTECODE=1

passed=0
failed=0

# expect <0|1> <description> <command...>
expect() {
    local want="$1" what="$2" got
    shift 2
    ( "$@" ) >"$work/last.out" 2>&1
    got=$?
    [ "$got" -eq 0 ] || got=1
    if [ "$got" -eq "$want" ]; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $what (exit $got, expected $want)"
        sed 's/^/    /' "$work/last.out"
    fi
}

# expect_out <description> <pattern>: the last command's output has the line.
expect_out() {
    if grep -q -E -- "$2" "$work/last.out"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $1 (no line matching $2)"
        sed 's/^/    /' "$work/last.out"
    fi
}

g() { git -C "$@"; }

base="$work/base"
new="$work/new"
for dir in "$base" "$new"; do
    git init -q "$dir"
    echo "$dir" > "$dir/flake.nix"
    mkdir "$dir/bin"
    python3 "$here/test_tail_eval.py" --write-carrier "$dir/bin/adk-verify-carrier-drc"
    g "$dir" add -A && g "$dir" commit -q -m one
done
fx="$work/fx"
export ADK_NIX_STORE_DIR="$fx/store"

# fixture [<base rev> <new rev>]: the files the stand-in nix serves.
fixture() {
    rm -rf "$fx" && mkdir -p "$fx"
    python3 "$here/test_tail_eval.py" --write-fixture "$fx" \
        "${1:-$(g "$base" rev-parse HEAD)}" "${2:-$(g "$new" rev-parse HEAD)}"
}

mkdir -p "$work/bin"
cat > "$work/bin/nix" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$work/nix.log"
[ ! -e "$work/nix.fail" ] || exit 1
case "\$1" in
    eval)
        for a in "\$@"; do
            case "\$a" in
                "$base#devShells."*) cat "$fx/base.shells.json"; exit 0 ;;
                "$base#packages."*) cat "$fx/base.packages.json"; exit 0 ;;
                "$new#devShells."*) cat "$fx/new.shells.json"; exit 0 ;;
                "$new#packages."*) cat "$fx/new.packages.json"; exit 0 ;;
            esac
        done
        exit 1 ;;
    derivation)
        shift 2
        exec python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(json.dumps({k: d[k] for k in sys.argv[2:]}))' \
            "$fx/all.show.json" "\$@" ;;
esac
exit 1
EOF
chmod +x "$work/bin/nix"
export PATH="$work/bin:$PATH"

# --- passes ----------------------------------------------------------------

fixture
: > "$work/nix.log"
expect 0 "the fixture passes through the check" bash "$check" "$base" "$new"
expect_out "the pass is the reader's" "tail eval: cli and build differ from $(g "$base" rev-parse HEAD) only in the revision they record"
expect 0 "every evaluation is offline, builds nothing and keeps the lock" \
    bash -c "[ \"\$(grep -c '^eval --json --offline --no-update-lock-file --max-jobs 0 --option allow-import-from-derivation false ' '$work/nix.log')\" -eq 4 ]"
expect 0 "nothing but eval and derivation show was run" \
    bash -c "! grep -v -E '^(eval|derivation show) ' '$work/nix.log'"
expect 0 "the shells are read by name, cli, build and tail only" \
    grep -q -F -- "--apply (names: set: builtins.mapAttrs (n: d: d.drvPath) (builtins.intersectAttrs names set)) { cli = 0; build = 0; tail = 0; }" "$work/nix.log"
expect 0 "two derivation shows per side" \
    bash -c "[ \"\$(grep -c '^derivation show ' '$work/nix.log')\" -eq 4 ]"

expect 0 "--keep writes what was compared" bash "$check" --keep "$work/kept" "$base" "$new"
for f in revs base.attrs new.attrs base.show new.show base.show-ci new.show-ci; do
    expect 0 "--keep holds $f.json" test -s "$work/kept/$f.json"
done
expect 0 "--keep holds the four store files tail-wrappers names" \
    bash -c "[ \"\$(ls '$work/kept/new.sources' | wc -l)\" -eq 4 ] &&
             cmp -s <(ls '$work/kept/new.sources') <(ls '$fx/store')"
expect 0 "--keep holds the checkout's carrier script" cmp "$work/kept/new.carrier-drc" "$new/bin/adk-verify-carrier-drc"
expect 0 "--keep records the store it read" bash -c "[ \"\$(cat '$work/kept/store-dir')\" = '$fx/store' ]"

# --- refusals --------------------------------------------------------------

rm "$fx/store/"*-adk-drc.in
expect 1 "a template missing from the store refuses" bash "$check" "$base" "$new"
expect_out "the refusal is the reader's" \
    "tail eval: unreadable: the template /nix/store/[0-9]+-adk-drc\.in was not kept"
fixture

echo "# changed" >> "$new/bin/adk-verify-carrier-drc"
g "$new" commit -q -a -m carrier
fixture
expect 1 "a checkout whose carrier script is not the store copy refuses" bash "$check" "$base" "$new"
expect_out "the refusal is the reader's" "is not the checkout's bin/adk-verify-carrier-drc"
g "$new" reset -q --hard HEAD~1
fixture

expect 1 "--keep into a directory that exists refuses" bash "$check" --keep "$work/kept" "$base" "$new"
expect_out "the refusal names it" "tail eval: $work/kept exists"

expect 1 "no base refuses" bash "$check"
expect_out "the refusal says so" "the base checkout is required"

: > "$work/nix.log"
echo change >> "$new/flake.nix"
expect 1 "a checkout with changes refuses" bash "$check" "$base" "$new"
expect_out "the refusal says why" "has changes; its flake would record no revision"
expect 0 "and nothing was evaluated" test ! -s "$work/nix.log"
g "$new" checkout -q flake.nix

touch "$base/untracked"
expect 1 "an untracked file in the base refuses" bash "$check" "$base" "$new"
rm "$base/untracked"

mkdir -p "$work/plain"
expect 1 "a directory that is not a checkout refuses" bash "$check" "$work/plain" "$new"

fixture "$(g "$base" rev-parse HEAD)" "$(printf 'e%.0s' {1..40})"
expect 1 "shells recording another revision than the checkout refuse" bash "$check" "$base" "$new"
expect_out "the refusal is the reader's" "tail eval: cli: the new shell records revision e{40}"

fixture
touch "$work/nix.fail"
expect 1 "a failing evaluation refuses" bash "$check" "$base" "$new"
expect_out "the refusal names the side" "evaluating the base shells failed"
rm "$work/nix.fail"

echo "check-tail-eval tests: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
