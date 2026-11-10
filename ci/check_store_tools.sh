#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The die build's tools in the runtime image's own /nix/store, checked in the
# image (verify stage) with no /nix mounted: a build step mounts nothing from
# the host. It reads the image's fixed ENV (OPENROAD, ADK_NIX_LIBRELANE,
# ADK_NIX_LIBRELANE_PYTHON, ADK_NIX_SG13G2_PDK and the launcher pair) and holds:
#
#   - each tool lies in a store path the image's closure list names;
#   - as nobody, LibreLane, OpenROAD and the launcher start;
#   - the PDK tree holds LibreLane's config.tcl, the interpreter is a regular
#     executable file and not a link, and the launcher is the file its ENV
#     sha256 names;
#   - the die flow's wrapper (bin/chiplet-die-flow) hands a build-die
#     PDK_ROOT=ADK_NIX_SG13G2_PDK, PDK=ihp-sg13g2, no LIBRELANE_CHECKOUT, and
#     the launcher pair unchanged: it is run as nobody with ADK_TOOLS at a copy
#     of nix/build-env.sh and a die flow that prints what it was handed;
#   - nothing under the store is writable as nobody, everything there is
#     root's, and no mount sits at /nix or below it.
#
# It prints each value it checked, so a reader sees them, not only a verdict.
#
# Usage: check_store_tools.sh --list FILE --nix-lib DIR --die-flow FILE
#            [--store DIR] [--mountinfo FILE] [--owner UID] [--as-self]
#        (--store, default /nix/store; --mountinfo, default /proc/self/mountinfo;
#         --owner, the store's owner, default 0; --as-self runs the nobody
#         steps as the caller: the self-test's)
#        check_store_tools.sh --self-test

set -euo pipefail

fail() {
    echo "store tools: $*" >&2
    exit 1
}

check() {
    local list="" lib="" die_flow="" store=/nix/store mountinfo=/proc/self/mountinfo owner=0
    local as_self=""
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --as-self ]; then
            as_self=1
            shift
            continue
        fi
        [ "$#" -ge 2 ] || fail "$1 needs a value"
        case "$1" in
            --list) list="$2" ;;
            --nix-lib) lib="$2" ;;
            --die-flow) die_flow="$2" ;;
            --store) store="$2" ;;
            --mountinfo) mountinfo="$2" ;;
            --owner) owner="$2" ;;
            *) fail "unknown argument $1" ;;
        esac
        shift 2
    done
    for v in list lib die_flow; do
        [ -n "${!v}" ] || fail "--${v//_/-} is required"
    done
    local name
    for name in OPENROAD ADK_NIX_LIBRELANE ADK_NIX_LIBRELANE_PYTHON ADK_NIX_SG13G2_PDK \
            CHIPLET_FLOW_LIBRELANE_LAUNCHER CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256; do
        [ -n "${!name:-}" ] || fail "the image's ENV sets no $name"
        echo "store tools: $name=${!name}"
    done
    local as=(setpriv --reuid=65534 --regid=65534 --clear-groups)
    [ -z "$as_self" ] || as=()
    local home
    home="$(mktemp -d "${TMPDIR:-/tmp}/store-tools.XXXXXX")"
    # shellcheck disable=SC2064
    trap "rm -rf '$home'" RETURN
    chmod 0755 "$home"
    # A HOME nobody can write, for whatever the tools cache there.
    mkdir "$home/home"
    chmod 1777 "$home/home"

    # Each tool's store path is one the closure list names.
    local path top
    for name in OPENROAD ADK_NIX_LIBRELANE ADK_NIX_LIBRELANE_PYTHON ADK_NIX_SG13G2_PDK; do
        path="${!name}"
        [[ "$path" == "$store"/* ]] || fail "$name ($path) is not under $store"
        top="$store/$(cut -d / -f 1 <<< "${path#"$store"/}")"
        # The list names /nix/store paths; --store moves only where they are read.
        grep -qxF -- "/nix/store/${top#"$store"/}" "$list" || \
            fail "$name's store path $top is not in $list"
        echo "store tools: $name lies in $top, which $list lists"
    done

    # They start, as nobody, from this store.
    local out
    out="$(env HOME="$home/home" "${as[@]}" "$ADK_NIX_LIBRELANE/bin/librelane" --version 2>&1)" || \
        fail "librelane --version failed: $(tail -n 3 <<< "$out")"
    echo "store tools: librelane --version: $(tail -n 1 <<< "$out")"
    out="$(env HOME="$home/home" "${as[@]}" "$OPENROAD" -version 2>&1)" || \
        fail "openroad -version failed: $(tail -n 3 <<< "$out")"
    echo "store tools: openroad -version: $(head -n 1 <<< "$out")"
    out="$(env HOME="$home/home" "${as[@]}" "$CHIPLET_FLOW_LIBRELANE_LAUNCHER" --version 2>&1)" || \
        fail "the launcher's --version failed: $(tail -n 3 <<< "$out")"
    echo "store tools: launcher --version: $(tail -n 1 <<< "$out")"

    [ -f "$ADK_NIX_SG13G2_PDK/ihp-sg13g2/libs.tech/librelane/config.tcl" ] || \
        fail "no config.tcl under $ADK_NIX_SG13G2_PDK/ihp-sg13g2/libs.tech/librelane"
    [ -f "$ADK_NIX_LIBRELANE_PYTHON" ] && [ -x "$ADK_NIX_LIBRELANE_PYTHON" ] && \
        [ ! -L "$ADK_NIX_LIBRELANE_PYTHON" ] || \
        fail "$ADK_NIX_LIBRELANE_PYTHON is not an executable regular file"
    local sha
    sha="$(sha256sum < "$CHIPLET_FLOW_LIBRELANE_LAUNCHER" | cut -d ' ' -f 1)"
    [ "$sha" = "$CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256" ] || \
        fail "$CHIPLET_FLOW_LIBRELANE_LAUNCHER is sha256 $sha, not its ENV's $CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256"

    # What the wrapper hands a build-die: the real wrapper and build-env.sh,
    # and a die flow that prints its environment, at the venv script of the
    # wrapper's own name, which is the one it execs.
    local script="$home/tools/venv/bin/${die_flow##*/}"
    mkdir -p "$home/tools/nix" "$home/tools/venv/bin"
    cp "$lib/build-env.sh" "$home/tools/nix/build-env.sh"
    cat > "$script" <<'SH'
#!/bin/sh
for v in PDK_ROOT PDK LIBRELANE_CHECKOUT CHIPLET_FLOW_LIBRELANE_LAUNCHER \
        CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256; do
    eval "set=\${$v+x} value=\${$v-}"
    if [ -n "$set" ]; then echo "$v=$value"; else echo "$v unset"; fi
done
SH
    chmod -R a+rX "$home/tools"
    chmod 0755 "$script"
    local handed want
    handed="$(env HOME="$home/home" ADK_TOOLS="$home/tools" LIBRELANE_CHECKOUT=/checkout \
        "${as[@]}" "$die_flow" build-die 2>&1)" || fail "$die_flow build-die did not run: $handed"
    want="PDK_ROOT=$ADK_NIX_SG13G2_PDK
PDK=ihp-sg13g2
LIBRELANE_CHECKOUT unset
CHIPLET_FLOW_LIBRELANE_LAUNCHER=$CHIPLET_FLOW_LIBRELANE_LAUNCHER
CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=$CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256"
    [ "$handed" = "$want" ] || fail "$die_flow hands a build-die $(paste -sd ' ' <<< "$handed"), not $(paste -sd ' ' <<< "$want")"
    while IFS= read -r line; do echo "store tools: build-die gets $line"; done <<< "$handed"

    # Read-only and root's, and nothing mounted at /nix.
    local found
    found="$("${as[@]}" find "$store" -writable -print -quit 2>&1)" || \
        fail "find over $store as nobody failed: $found"
    [ -z "$found" ] || fail "$found is writable as nobody"
    found="$(find "$store" \( ! -user "$owner" -o ! -group "$owner" \) -print -quit)"
    [ -z "$found" ] || fail "$found is not uid $owner's"
    found="$(awk '$5 == "/nix" || index($5, "/nix/") == 1 { print $5; exit }' "$mountinfo")"
    [ -z "$found" ] || fail "$found is a mount"
    echo "store tools: $(grep -c '^/' "$list") listed paths; nothing under $store writable" \
        "as nobody, all of it uid $owner's, no mount at /nix"
}

self_test() {
    local work here passed=0 uid owner=""
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    uid="$(id -u)"
    work="$(mktemp -d "${TMPDIR:-/tmp}/store-tools-test.XXXXXX")"
    work="$(realpath "$work")"
    # shellcheck disable=SC2064
    trap "chmod -R u+w '$work'; rm -rf '$work'" EXIT
    local store="$work/store"
    local openroad="$store/$(printf 'o%.0s' {1..32})-openroad-x"
    local librelane="$store/$(printf 'l%.0s' {1..32})-python3.13-librelane-x"
    local python_pkg="$store/$(printf 'p%.0s' {1..32})-python3-3.13.9-env"
    local pdk="$store/$(printf 'h%.0s' {1..32})-ihp-open-pdk-x"
    mkdir -p "$openroad/bin" "$librelane/bin" "$python_pkg/bin" \
        "$pdk/ihp-sg13g2/libs.tech/librelane" "$work/launcher"
    printf '#!/bin/sh\necho "OpenROAD v-test"\n' > "$openroad/bin/openroad"
    printf '#!/bin/sh\necho "LibreLane v-test"\n' > "$librelane/bin/librelane"
    printf '#!/bin/sh\n' > "$python_pkg/bin/python3.13"
    ln -s python3.13 "$python_pkg/bin/python3"
    touch "$pdk/ihp-sg13g2/libs.tech/librelane/config.tcl"
    chmod 0555 "$openroad/bin/openroad" "$librelane/bin/librelane" "$python_pkg/bin/python3.13"
    printf '#!/bin/sh\necho "launcher v-test"\n' > "$work/launcher/librelane"
    chmod 0555 "$work/launcher/librelane"
    # The list names store paths as /nix/store/<name>, as the image's does.
    printf '# the closure\n' > "$work/list"
    for p in "$librelane" "$openroad" "$pdk" "$python_pkg"; do
        echo "/nix/store/${p##*/}" >> "$work/list"
    done
    : > "$work/mountinfo"
    chmod -R a-w "$store"
    local sha
    sha="$(sha256sum < "$work/launcher/librelane" | cut -d ' ' -f 1)"
    run() {
        (export OPENROAD="$openroad/bin/openroad" ADK_NIX_LIBRELANE="$librelane" \
            ADK_NIX_LIBRELANE_PYTHON="$python_pkg/bin/python3.13" ADK_NIX_SG13G2_PDK="$pdk" \
            CHIPLET_FLOW_LIBRELANE_LAUNCHER="$work/launcher/librelane" \
            CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256="$sha" PDK_ROOT=/opt/adk-tools/IHP-Open-PDK
         "$@"
         TMPDIR="$work" check --list "$work/list" --nix-lib "$here/nix" \
            --die-flow "$here/bin/chiplet-die-flow" --store "$store" --mountinfo "$work/mountinfo" \
            --owner "${owner:-$uid}" --as-self) > "$work/out" 2>&1
    }
    expect() {
        local want="$1" what="$2" rc=0
        shift 2
        run "$@" || rc=$?
        if { [ "$want" = ok ] && [ "$rc" -eq 0 ]; } || \
                { [ "$want" != ok ] && [ "$rc" -ne 0 ] && grep -q -F -- "$want" "$work/out"; }; then
            passed=$((passed + 1))
        else
            echo "FAIL: $what (exit $rc)"; sed 's/^/    /' "$work/out"; exit 1
        fi
    }
    expect ok "the image's shape" true
    grep -q -F "store tools: build-die gets PDK_ROOT=$pdk" "$work/out" && \
        grep -q -F "store tools: build-die gets LIBRELANE_CHECKOUT unset" "$work/out" && \
        grep -q -F "store tools: openroad -version: OpenROAD v-test" "$work/out" || \
        { echo "FAIL: the values are printed"; cat "$work/out"; exit 1; }
    passed=$((passed + 1))
    expect "the image's ENV sets no ADK_NIX_SG13G2_PDK" "an ENV without the PDK" \
        unset ADK_NIX_SG13G2_PDK
    expect "is not in $work/list" "a tool outside the list" \
        export OPENROAD="$store/$(printf 'z%.0s' {1..32})-openroad-y/bin/openroad"
    expect "is not under $store" "a tool outside the store" export OPENROAD=/usr/bin/true
    expect "not its ENV's" "a launcher of another sha256" \
        export CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256="$(printf '0%.0s' {1..64})"
    expect "is not an executable regular file" "an interpreter that is a link" \
        export ADK_NIX_LIBRELANE_PYTHON="$python_pkg/bin/python3"
    expect "hands a build-die" "a wrapper that keeps another PDK_ROOT" export PDK_ROOT=/elsewhere
    expect "librelane --version failed" "a LibreLane that does not start" \
        export ADK_NIX_LIBRELANE="$openroad"
    chmod u+w "$pdk/ihp-sg13g2/libs.tech/librelane"
    mv "$pdk/ihp-sg13g2/libs.tech/librelane/config.tcl" "$work/config.tcl"
    chmod u-w "$pdk/ihp-sg13g2/libs.tech/librelane"
    expect "no config.tcl" "a PDK without LibreLane's files" true
    chmod u+w "$pdk/ihp-sg13g2/libs.tech/librelane"
    mv "$work/config.tcl" "$pdk/ihp-sg13g2/libs.tech/librelane/config.tcl"
    chmod u-w "$pdk/ihp-sg13g2/libs.tech/librelane"
    chmod u+w "$pdk"
    expect "$pdk is writable as nobody" "a writable store path" true
    chmod u-w "$pdk"
    owner=$((uid + 1))
    expect "is not uid $((uid + 1))'s" "a store of another owner" true
    unset owner
    echo "x x x / /nix rw - - -" > "$work/mountinfo"
    expect "/nix is a mount" "a /nix mount" true
    : > "$work/mountinfo"
    echo "check_store_tools self-test: $passed passed"
}

if [ "${1:-}" = --self-test ]; then
    self_test
else
    check "$@"
fi
