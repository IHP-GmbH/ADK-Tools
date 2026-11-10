#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The die-build environment of the runtime image, checked in the image (verify
# stage): what packaging-flow starts for a die, and the LibreLane launcher that
# build runs.
#
#   - packaging-flow finds the die flow by its program name on PATH; that is
#     the image's wrapper (bin/chiplet-die-flow, which reads the store PDK for a
#     build-die), not the venv's console script behind it.
#   - The launcher build.sh wrote with nix/build-env.sh is a regular file, not
#     a link, root's, mode 0555, with the sha256 build.sh passed.
#   - Its PYTHONPATH is, in order, the roots nix/build-env.sh's
#     adk_nix_launcher_roots resolves from the staged trees, each checked to
#     hold the packages it is there for, and each exactly
#     <staging>/<name>/<subdirectory> with nothing resolved through a link: the
#     path an overlay of <name> mounts at, so an overlay's code is what
#     LibreLane imports.
#
# The roots come from build-env.sh's own functions (sourced with
# ADK_NIX_LIB_ONLY=1, with cli-env.sh for the two helpers they call), so the
# package checks are written once.
#
# Usage: check_die_build_env.sh --launcher FILE --sha256 HEX --python PY
#            --nix-lib DIR --staging DIR --die-flow FILE [--owner UID]
#        (--owner: the launcher's owner, default 0; the self-test's is its own)
#        check_die_build_env.sh --self-test

set -euo pipefail

fail() {
    echo "die build env: $*" >&2
    exit 1
}

check() {
    local launcher="" sha="" python="" lib="" staging="" die_flow="" owner=0
    while [ "$#" -gt 0 ]; do
        [ "$#" -ge 2 ] || fail "$1 needs a value"
        case "$1" in
            --launcher) launcher="$2" ;;
            --sha256) sha="$2" ;;
            --python) python="$2" ;;
            --nix-lib) lib="$2" ;;
            --staging) staging="$2" ;;
            --die-flow) die_flow="$2" ;;
            --owner) owner="$2" ;;
            *) fail "unknown argument $1" ;;
        esac
        shift 2
    done
    for v in launcher sha python lib staging die_flow; do
        [ -n "${!v}" ] || fail "--${v//_/-} is required"
    done
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || fail "--sha256 '$sha' is not a sha256"

    # The die flow packaging-flow starts: its own lookup, by its own name.
    local found
    found="$("$python" -B -c 'import shutil; from packaging_flow.runner import DIE_FLOW_PROGRAM
print(shutil.which(DIE_FLOW_PROGRAM) or "")')" || fail "packaging-flow's die flow lookup did not run"
    [ "$found" = "$die_flow" ] || fail "packaging-flow finds the die flow at '$found', not $die_flow"

    [ -f "$launcher" ] && [ ! -L "$launcher" ] || fail "$launcher is not a regular file"
    [ "$(stat -c '%u %a' -- "$launcher")" = "$owner 555" ] || \
        fail "$launcher is $(stat -c 'uid %u mode %a' -- "$launcher"), not uid $owner's and 0555"
    local got
    got="$(sha256sum < "$launcher" | cut -d ' ' -f 1)"
    [ "$got" = "$sha" ] || fail "$launcher is sha256 $got, not $sha"

    # shellcheck disable=SC1091
    ADK_NIX_LIB_ONLY=1 . "$lib/cli-env.sh"
    # shellcheck disable=SC1091
    ADK_NIX_LIB_ONLY=1 . "$lib/build-env.sh"
    declare -F adk_nix_die_build_env > /dev/null || \
        fail "$lib/build-env.sh defines no adk_nix_die_build_env, which bin/chiplet-die-flow calls"
    local trees="" expected name roots pythonpath
    while IFS='|' read -r name _; do
        [ -z "$name" ] || trees+="$name|$staging/$name"$'\n'
    done <<< "$ADK_NIX_LAUNCHER_TREES"
    expected="$(adk_nix_staged_roots "$staging")"$'\n'
    roots="$(adk_nix_launcher_roots "$trees")" || fail "the staged trees do not hold the launcher's packages"
    [ "$roots"$'\n' = "$expected" ] || \
        fail "the roots resolve through a link: $(paste -sd ' ' <<< "$roots"), not $(paste -sd ' ' <<< "$expected")"
    pythonpath="$(grep '^export PYTHONPATH=' "$launcher")" || fail "$launcher sets no PYTHONPATH"
    [ "$pythonpath" = "export PYTHONPATH='$(paste -sd ':' <<< "$roots")'" ] || \
        fail "$launcher has $pythonpath, not the roots $(paste -sd ':' <<< "$roots")"
    echo "die build env: $die_flow is packaging-flow's die flow; launcher sha256 $sha," \
        "PYTHONPATH $(paste -sd ':' <<< "$roots")"
}

self_test() {
    local work here passed=0
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    work="$(mktemp -d "${TMPDIR:-/tmp}/die-build-env.XXXXXX")"
    work="$(realpath "$work")"
    # shellcheck disable=SC2064
    trap "rm -rf '$work'" EXIT
    local staging="$work/staging" store="$work/store"
    local librelane="$store/$(printf 'l%.0s' {1..32})-python3.13-librelane-x"
    local bash_pkg="$store/$(printf 'b%.0s' {1..32})-bash-5.3"
    mkdir -p "$librelane/bin" "$bash_pkg/bin" "$work/bin" "$work/py/packaging_flow"
    printf '#! %s/bin/bash -e\n' "$bash_pkg" > "$librelane/bin/librelane"
    chmod 0555 "$librelane/bin/librelane"
    mkdir -p "$staging/chiplet-flow/src/chiplet_die_flow" "$staging/chiplet-flow/src/librelane_plugin_chiplet" \
        "$staging/chiplet-spec/reference/python/chiplet_format_io"
    touch "$staging/chiplet-flow/src/chiplet_die_flow/__init__.py" \
        "$staging/chiplet-flow/src/librelane_plugin_chiplet/__init__.py" \
        "$staging/chiplet-spec/reference/python/chiplet_format_io/__init__.py"
    printf '' > "$work/py/packaging_flow/__init__.py"
    printf 'DIE_FLOW_PROGRAM = "chiplet-die-flow"\n' > "$work/py/packaging_flow/runner.py"
    printf '#!/bin/sh\n' > "$work/bin/chiplet-die-flow"
    chmod 0755 "$work/bin/chiplet-die-flow"
    # The stand-in python: the host's, with the fake packaging_flow first.
    printf '#!/bin/sh\nPYTHONPATH=%s exec python3 "$@"\n' "$work/py" > "$work/python"
    chmod 0755 "$work/python"
    local text
    text="$(ADK_NIX_LIB_ONLY=1 ADK_NIX_STORE_DIR="$store"; . "$here/nix/cli-env.sh"
            ADK_NIX_LIB_ONLY=1; . "$here/nix/build-env.sh"
            adk_nix_launcher_text "$librelane" "$staging/chiplet-flow/src" \
                "$staging/chiplet-spec/reference/python")"
    printf '%s\n' "$text" > "$work/launcher"
    chmod 0555 "$work/launcher"
    local sha uid
    sha="$(sha256sum < "$work/launcher" | cut -d ' ' -f 1)"
    uid="$(id -u)"
    run() {
        (PATH="$work/bin:/usr/bin:/bin" check --launcher "$work/launcher" --sha256 "$sha" \
            --python "$work/python" --nix-lib "$here/nix" --staging "$staging" \
            --die-flow "$work/bin/chiplet-die-flow" "$@") > "$work/out" 2>&1
    }
    expect() {
        local want="$1" what="$2" rc=0
        shift 2
        "$@" || rc=$?
        if { [ "$want" = ok ] && [ "$rc" -eq 0 ]; } || \
                { [ "$want" != ok ] && [ "$rc" -ne 0 ] && grep -q -F -- "$want" "$work/out"; }; then
            passed=$((passed + 1))
        else
            echo "FAIL: $what (exit $rc)"; sed 's/^/    /' "$work/out"; exit 1
        fi
    }
    expect ok "the image's shape" run --owner "$uid"
    expect "not uid $((uid + 1))'s and 0555" "a launcher of another owner" run --owner "$((uid + 1))"
    chmod 0755 "$work/launcher"
    expect "not uid $uid's and 0555" "a launcher its owner can write" run --owner "$uid"
    chmod 0555 "$work/launcher"
    expect "is sha256" "another sha256" run --owner "$uid" --sha256 "$(printf '0%.0s' {1..64})"
    mv "$work/bin/chiplet-die-flow" "$work/chiplet-die-flow"
    expect "finds the die flow at ''" "no die flow on PATH" run --owner "$uid"
    mv "$work/chiplet-die-flow" "$work/bin/chiplet-die-flow"
    rm "$staging/chiplet-spec/reference/python/chiplet_format_io/__init__.py"
    expect "do not hold the launcher's packages" "a tree without its package" run --owner "$uid"
    touch "$staging/chiplet-spec/reference/python/chiplet_format_io/__init__.py"
    mv "$staging/chiplet-spec" "$work/elsewhere"
    ln -s "$work/elsewhere" "$staging/chiplet-spec"
    expect "resolve through a link" "a staged tree that is a link" run --owner "$uid"
    rm "$staging/chiplet-spec"
    mv "$work/elsewhere" "$staging/chiplet-spec"
    expect "is not a regular file" "no launcher" run --owner "$uid" --launcher "$work/nowhere"
    expect "--owner needs a value" "an option without its value" run --owner
    mkdir "$work/nix"
    cp "$here/nix/cli-env.sh" "$work/nix/"
    sed 's/^adk_nix_die_build_env() {/adk_nix_other() {/' "$here/nix/build-env.sh" > "$work/nix/build-env.sh"
    expect "defines no adk_nix_die_build_env" "a build-env.sh without the wrapper's function" \
        run --owner "$uid" --nix-lib "$work/nix"
    ln -s "$work/launcher" "$work/link"
    expect "is not a regular file" "a launcher that is a link" run --owner "$uid" --launcher "$work/link"
    # A launcher written for the same roots in another order: its own sha256
    # passes, and the order refuses.
    text="$(ADK_NIX_LIB_ONLY=1 ADK_NIX_STORE_DIR="$store"; . "$here/nix/cli-env.sh"
            ADK_NIX_LIB_ONLY=1; . "$here/nix/build-env.sh"
            adk_nix_launcher_text "$librelane" "$staging/chiplet-spec/reference/python" \
                "$staging/chiplet-flow/src")"
    printf '%s\n' "$text" > "$work/reordered"
    chmod 0555 "$work/reordered"
    expect "not the roots" "the roots in another order" run --owner "$uid" \
        --launcher "$work/reordered" --sha256 "$(sha256sum < "$work/reordered" | cut -d ' ' -f 1)"
    echo "check_die_build_env self-test: $passed passed"
}

if [ "${1:-}" = --self-test ]; then
    self_test
else
    check "$@"
fi
