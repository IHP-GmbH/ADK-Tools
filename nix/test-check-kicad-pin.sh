#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for nix/check-kicad-pin.sh, without Nix or network. The check runs
# against a stand-in adk-tools checkout (a tools/kicad gitlink, .gitmodules and
# flake.lock), with a `nix` and a `curl` on PATH that record any call, so a
# pass also shows the check called neither. Each refusal is reached by
# breaking exactly one thing against a setup that passes.
#
# Needs bash, git, python3 and coreutils. Writes only under one mktemp
# directory.
#
# Usage: nix/test-check-kicad-pin.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
check="$here/check-kicad-pin.sh"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-kicad-pin-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
work="$(realpath "$work")"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid

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

# A `nix` and a `curl` that only record that they were called.
mkdir -p "$work/bin"
for tool in nix curl; do
    printf '#!/bin/sh\necho "%s $*" >> "%s/called"\nexit 1\n' "$tool" "$work" > "$work/bin/$tool"
    chmod +x "$work/bin/$tool"
done
export PATH="$work/bin:$PATH"

# The fork: one commit, and a second so "the gitlink" is not "the latest".
fork="$work/up/KiCad-ADK-MOD"
git init -q "$fork"
echo one > "$fork/README"
g "$fork" add -A && g "$fork" commit -q -m one
rev="$(g "$fork" rev-parse HEAD)"
echo two > "$fork/README"
g "$fork" commit -q -am two
later="$(g "$fork" rev-parse HEAD)"

# modules <url> [<path>]: .gitmodules with one kicad entry, and a second,
# unrelated submodule so the path, not the first entry, decides.
modules() {
    cat <<EOF
[submodule "tools/adk"]
	path = tools/adk
	url = https://github.com/IHP-GmbH/ADK.git
[submodule "tools/kicad"]
	path = ${2:-tools/kicad}
	url = $1
	branch = dev
EOF
}

# lock <rev> [<owner> <repo> <type> <narHash> <name>]: a flake.lock with the
# kicad-fork root input and one other node.
lock() {
    python3 - "$@" <<'EOF'
import json, sys
a = sys.argv[1:] + [None] * 6
rev, owner, repo, typ, nar, name = a[:6]
node = {"locked": {"lastModified": 1782994811, "narHash": nar or "sha256-AAAA",
                   "owner": owner or "IHP-GmbH", "repo": repo or "KiCad-ADK-MOD",
                   "rev": rev, "type": typ or "github"},
        "original": {"owner": "IHP-GmbH", "repo": "KiCad-ADK-MOD", "rev": rev, "type": "github"},
        "flake": False}
if nar == "none":
    del node["locked"]["narHash"]
nodes = {"kicad-fork": node, "nix-eda": {"locked": {"type": "github", "rev": "0" * 40}},
         "root": {"inputs": {(name or "kicad-fork"): "kicad-fork", "nix-eda": "nix-eda"}}}
print(json.dumps({"nodes": nodes, "root": "root", "version": 7}, indent=2))
EOF
}

# setup <dir> <gitlink rev> <.gitmodules url> [<.gitmodules path>]: a
# committed stand-in checkout; flake.lock is written separately.
setup() {
    local dir="$1" link="$2" url="$3" mpath="${4:-tools/kicad}"
    rm -rf "$dir"
    git init -q "$dir"
    mkdir -p "$dir/nix"
    cp "$check" "$here/cli-env.sh" "$dir/nix/"
    modules "$url" "$mpath" > "$dir/.gitmodules"
    g "$dir" add -A
    [ -z "$link" ] || g "$dir" update-index --add --cacheinfo "160000,$link,tools/kicad"
    g "$dir" commit -q -m base
}

good_url=https://github.com/IHP-GmbH/KiCad-ADK-MOD.git
at="$work/at"

# --- passes ----------------------------------------------------------------

setup "$at" "$rev" "$good_url"
lock "$rev" > "$at/flake.lock"
expect 0 "the gitlink, its .gitmodules repository and the lock agree" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the pass names the rev and the repository" "kicad pin: flake.lock's kicad-fork is $rev from IHP-GmbH/KiCad-ADK-MOD"
expect 0 "the default checkout is the script's own" bash "$at/nix/check-kicad-pin.sh"
expect 1 "neither nix nor curl was called" test -e "$work/called"

for url in https://github.com/IHP-GmbH/KiCad-ADK-MOD https://github.com/IHP-GmbH/KiCad-ADK-MOD/ \
           git@github.com:IHP-GmbH/KiCad-ADK-MOD.git ssh://git@github.com/IHP-GmbH/KiCad-ADK-MOD.git \
           https://github.com/ihp-gmbh/kicad-adk-mod.git; do
    setup "$at" "$rev" "$url"
    lock "$rev" > "$at/flake.lock"
    expect 0 "the URL form $url names the same repository" bash "$at/nix/check-kicad-pin.sh" "$at"
done

# --- refusals, one break each ----------------------------------------------

setup "$at" "$rev" "$good_url"
lock "$later" > "$at/flake.lock"
expect 1 "a lock at another commit of the fork refuses" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the rev refusal names both" "kicad-fork is $later, the tools/kicad gitlink is $rev"

setup "$at" "$later" "$good_url"
lock "$rev" > "$at/flake.lock"
expect 1 "a gitlink moved without the lock refuses" bash "$at/nix/check-kicad-pin.sh" "$at"

setup "$at" "$rev" "$good_url"
lock "$rev" KiCad KiCad-ADK-MOD > "$at/flake.lock"
expect 1 "a lock from another owner refuses" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the repository refusal names both" "is from KiCad/KiCad-ADK-MOD, .gitmodules names IHP-GmbH/KiCad-ADK-MOD"

lock "$rev" IHP-GmbH kicad > "$at/flake.lock"
expect 1 "a lock from another repository of the same owner refuses" bash "$at/nix/check-kicad-pin.sh" "$at"

setup "$at" "$rev" https://github.com/KiCad/KiCad-ADK-MOD.git
lock "$rev" > "$at/flake.lock"
expect 1 ".gitmodules naming another repository refuses" bash "$at/nix/check-kicad-pin.sh" "$at"

setup "$at" "$rev" https://gitlab.com/IHP-GmbH/KiCad-ADK-MOD.git
lock "$rev" > "$at/flake.lock"
expect 1 "a .gitmodules URL that is not GitHub refuses" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the URL refusal says so" "is not a GitHub repository"

setup "$at" "$rev" "$good_url" tools/kicad-fork
lock "$rev" > "$at/flake.lock"
expect 1 ".gitmodules with no entry at tools/kicad refuses" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the path refusal counts the entries" "has 0 submodules at tools/kicad, expected 1"

setup "$at" "$rev" "$good_url"
{ modules "$good_url"; printf '[submodule "kicad-copy"]\n\tpath = tools/kicad\n\turl = %s\n' "$good_url"; } > "$at/.gitmodules"
g "$at" add .gitmodules && g "$at" commit -q -m "two entries"
lock "$rev" > "$at/flake.lock"
expect 1 "two .gitmodules entries at tools/kicad refuse" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the path refusal counts both" "has 2 submodules at tools/kicad, expected 1"

setup "$at" "" "$good_url"
lock "$rev" > "$at/flake.lock"
expect 1 "no gitlink at tools/kicad refuses" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the gitlink refusal says so" "no gitlink at tools/kicad"

setup "$at" "" "$good_url"
mkdir -p "$at/tools" && echo "$rev" > "$at/tools/kicad"
g "$at" add -A && g "$at" commit -q -m "a file at the path"
lock "$rev" > "$at/flake.lock"
expect 1 "a regular file at tools/kicad is not a gitlink" bash "$at/nix/check-kicad-pin.sh" "$at"

setup "$at" "$rev" "$good_url"
g "$at" update-index --cacheinfo "160000,$later,tools/kicad"
lock "$rev" > "$at/flake.lock"
expect 0 "a gitlink staged but not committed does not count; HEAD's does" bash "$at/nix/check-kicad-pin.sh" "$at"

setup "$at" "$rev" "$good_url"
lock "$rev" "" "" "" "" kicad > "$at/flake.lock"
expect 1 "a lock without the kicad-fork root input refuses" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "the input refusal names it" "has no kicad-fork root input"

lock "$rev" "" "" git > "$at/flake.lock"
expect 1 "an input that is not type github refuses" bash "$at/nix/check-kicad-pin.sh" "$at"

lock "$rev" "" "" "" none > "$at/flake.lock"
expect 1 "an input without a narHash refuses" bash "$at/nix/check-kicad-pin.sh" "$at"

echo '{"nodes": ' > "$at/flake.lock"
expect 1 "an unreadable flake.lock refuses" bash "$at/nix/check-kicad-pin.sh" "$at"
expect_out "and says it cannot read it" "cannot read"

rm -f "$at/flake.lock"
expect 1 "a missing flake.lock refuses" bash "$at/nix/check-kicad-pin.sh" "$at"

rm -rf "$at/.git"
lock "$rev" > "$at/flake.lock"
expect 1 "a directory that is not a git checkout refuses" bash "$at/nix/check-kicad-pin.sh" "$at"

expect 1 "neither nix nor curl was called by any case" test -e "$work/called"

echo "check-kicad-pin: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
