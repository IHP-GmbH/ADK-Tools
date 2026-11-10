# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Sourced by the shellHook of devShells.build (flake.nix), after cli-env.sh has
# made the CLI shell; not run on its own. With ADK_NIX_LIB_ONLY=1 it only
# defines its functions: that is how the image's bin/chiplet-die-flow (a copy at
# /opt/adk-tools/nix), build.sh and the verify checks source it. It adds what a
# die build needs on top of that shell and nothing else:
#
#   PDK_ROOT, PDK   the materialized sg13g2 tree from the flake. cli-env.sh
#                   clears both with the rest of the host's variables; they are
#                   set again here from what the flake declared.
#   the launcher    a short script that runs the flake's LibreLane with the
#                   flow's checked sources on its import path, exported as
#                   CHIPLET_FLOW_LIBRELANE_LAUNCHER with its sha256 as
#                   CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256, which chiplet-flow
#                   binds and records for every die build.
#
# It also unsets LIBRELANE_CHECKOUT: the launcher is the only way LibreLane
# runs here.
#
# Why a launcher: the flow's packages are installed only in the CLI shell's
# venv, and the store LibreLane runs its own interpreter, which does not see
# that venv. The launcher runs the store package's bin/librelane, the wrapper
# that puts LibreLane's tools on PATH, so nothing of the packaged environment
# is dropped; it adds the source trees the CLI shell resolved and verified, by
# PYTHONPATH, where LibreLane finds librelane_plugin_* when it starts. Every
# path it adds or runs is written in it, so its sha256 binds them. Both the
# launcher and the wrapper run under bash -p, which reads no BASH_ENV and
# imports no function from the environment, so nothing the caller's shell
# sources or exports can put a variable back after the launcher drops it.
#
# In evidence mode it then runs nix/capture_build_context.py, which asks the
# launcher what it imports and writes build-context.json beside the CLI
# shell's nix-context.json; a problem there refuses the shell.
#
# Inputs from flake.nix: ADK_NIX_LIBRELANE (the LibreLane package's store
# path), ADK_NIX_SG13G2_PDK (the materialized PDK's store path), ADK_NIX_LIB.
# From cli-env.sh: ADK_NIX_TREES, ADK_NIX_STATE, ADK_NIX_MODE, and in evidence
# mode ADK_NIX_VENV and ADK_NIX_CONTEXT_OUT. ADK_NIX_STORE_DIR (default
# /nix/store) exists for nix/test-build-env.sh; evidence mode refuses any other
# value.

ADK_NIX_STORE_DIR="${ADK_NIX_STORE_DIR:-/nix/store}"

# The trees whose sources LibreLane has to import, and the directory in each
# that goes on its path: the plugin and the flow it registers, and the package
# the flow imports (chiplet-spec's reader from reference/python, the directory
# pip installs it from). The die flow imports no system description, and its own
# tests fail if it does, so the description's tree is not one of these.
ADK_NIX_LAUNCHER_TREES="
chiplet-flow|src|chiplet_die_flow librelane_plugin_chiplet
chiplet-spec|reference/python|chiplet_format_io
"

# The launcher's roots in a tree of staged checkouts, one line each:
# <staging>/<name>/<subdirectory>, the path an overlay of <name> mounts at in
# the image. build.sh writes the image's launcher over these, and the verify
# stage holds the resolved roots to them.
adk_nix_staged_roots() {
    local staging="$1" name sub
    while IFS='|' read -r name sub _; do
        [ -z "$name" ] || echo "$staging/$name/$sub"
    done <<< "$ADK_NIX_LAUNCHER_TREES"
}

# The PDK a die build reads from a materialized sg13g2 tree: PDK_ROOT is the
# tree, PDK is ihp-sg13g2, and no LIBRELANE_CHECKOUT is handed on (the flow
# takes one for its nix-shell dispatch; a die build here runs LibreLane through
# the launcher only). Returns 1, setting nothing, for a tree without LibreLane's
# files. The build shell below and the image's bin/chiplet-die-flow both set it
# here, so the two are one copy.
adk_nix_die_build_env() {
    [ -d "$1/ihp-sg13g2/libs.tech/librelane" ] || return 1
    export PDK_ROOT="$1" PDK=ihp-sg13g2
    unset LIBRELANE_CHECKOUT
}

# A path the launcher can carry literally: absolute, and nothing a shell or
# PYTHONPATH would read differently (no quote, space, colon or newline).
adk_nix_plain_path() {
    [[ "$1" =~ ^/[A-Za-z0-9._/+@-]+$ ]] && [[ "$1" != *//* ]] && [[ "/$1/" != */../* ]] \
        && [[ "/$1/" != */./* ]]
}

# The bash that runs the wrapper: its first line, which makeWrapper writes as
# `#! /nix/store/<hash>-bash-<version>/bin/bash -e`. The launcher uses the same
# one, so it adds nothing outside LibreLane's closure, and runs the wrapper
# with it as that line would, with -e.
adk_nix_wrapper_bash() {
    local wrapper="$1" first bash
    IFS= read -r first < "$wrapper" || return 1
    [[ "$first" =~ ^#![[:space:]]?([^[:space:]]+)[[:space:]]+-e$ ]] || return 1
    bash="${BASH_REMATCH[1]}"
    adk_nix_store_path "${bash%/bin/bash}" && [[ "$bash" == */bin/bash ]] \
        && [[ "${bash%/bin/bash}" =~ -bash-[^/]+$ ]] || return 1
    echo "$bash"
}

# A top-level store path, written literally: <store>/<32 hash chars>-<name>.
adk_nix_store_path() {
    local rest="${1#"$ADK_NIX_STORE_DIR"/}"
    [ "$rest" != "$1" ] && [[ "$rest" =~ ^[0-9a-z]{32}-[^/]+$ ]] && adk_nix_plain_path "$1"
}

# adk_nix_launcher_text <librelane store path> <root>...: the launcher, on
# stdout. Refuses (exit 1) anything it could not write down literally. It reads
# LibreLane's wrapper, which has to be here, but not the roots: they can be
# paths of another filesystem, as build.sh writes the image's launcher for the
# image's staged trees. Whoever writes the file for this filesystem checks them
# (adk_nix_write_launcher).
adk_nix_launcher_text() {
    local librelane="$1" bash root pythonpath=""
    shift
    adk_nix_store_path "$librelane" || adk_nix_die "launcher: $librelane is not a store path"
    [ -x "$librelane/bin/librelane" ] && [ -f "$librelane/bin/librelane" ] || \
        adk_nix_die "launcher: no executable $librelane/bin/librelane"
    bash="$(adk_nix_wrapper_bash "$librelane/bin/librelane")" || \
        adk_nix_die "launcher: $librelane/bin/librelane is not the bash wrapper makeWrapper writes"
    [ "$#" -gt 0 ] || adk_nix_die "launcher: no source roots"
    for root in "$@"; do
        adk_nix_plain_path "$root" || adk_nix_die "launcher: source root $root cannot be written literally"
        case ":$pythonpath:" in
            *":$root:"*) adk_nix_die "launcher: source root $root named twice" ;;
        esac
        pythonpath="${pythonpath:+$pythonpath:}$root"
    done
    cat <<EOF
#!$bash -p
# Written by adk-tools nix/build-env.sh. Runs this LibreLane with these flow
# sources on its import path and nothing else from the caller's; this file's
# sha256 names both. Every PYTHON* and NIX_PYTHON* variable the caller set is
# dropped (Nix's sitecustomize adds NIX_PYTHONPATH's directories, .pth files
# included), except PYTHONVERBOSE, which only traces imports. -p, here and for
# the wrapper: no BASH_ENV or ENV is sourced and no function is imported.
unset BASH_ENV ENV
for v in "\${!PYTHON@}" "\${!NIX_PYTHON@}"; do [ "\$v" = PYTHONVERBOSE ] || unset "\$v"; done
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH='$pythonpath'
exec '$bash' -p -e '$librelane/bin/librelane' "\$@"
EOF
}

# adk_nix_write_launcher <dir> <librelane store path> <root>...: writes
# <dir>/librelane read-only and executable, replacing whatever was there, and
# prints its sha256. Each root has to be a directory here, where the launcher
# runs. The file is written beside its final name and renamed over it, so a
# reader never sees half of it, and read back before the digest is trusted.
adk_nix_write_launcher() {
    local dir="$1" text tmp sha root
    shift
    adk_nix_plain_path "$dir" || adk_nix_die "launcher: $dir cannot be written literally"
    for root in "${@:2}"; do
        [ -d "$root" ] || adk_nix_die "launcher: source root $root is not a directory"
    done
    text="$(adk_nix_launcher_text "$@")" || exit 1
    mkdir -p -- "$dir" || adk_nix_die "launcher: cannot create $dir"
    tmp="$(mktemp "$dir/.librelane.XXXXXX")" || adk_nix_die "launcher: cannot write in $dir"
    printf '%s\n' "$text" > "$tmp" && chmod 0555 "$tmp" && mv -f -- "$tmp" "$dir/librelane" || {
        rm -f -- "$tmp"
        adk_nix_die "launcher: cannot write $dir/librelane"
    }
    [ "$(cat -- "$dir/librelane")" = "$text" ] || adk_nix_die "launcher: $dir/librelane reads back different"
    sha="$(sha256sum -- "$dir/librelane" | cut -d' ' -f1)"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || adk_nix_die "launcher: cannot digest $dir/librelane"
    echo "$sha"
}

# The launcher's source roots, from the trees the CLI shell resolved: one line
# per root, each checked to hold the packages it is there for. A tree the CLI
# shell installs from two directories has two lines there, which name one tree.
adk_nix_launcher_roots() {
    local trees="$1" name sub pkgs tree pkg found
    while IFS='|' read -r name sub pkgs; do
        [ -n "$name" ] || continue
        found=""
        tree="$(awk -F'|' -v n="$name" '$1 == n { print $2 }' <<< "$trees" | sort -u)"
        [ -n "$tree" ] || adk_nix_die "launcher: the CLI shell resolved no $name tree"
        [[ "$tree" != *$'\n'* ]] || \
            adk_nix_die "launcher: the CLI shell resolved $name to more than one tree"
        for pkg in $pkgs; do
            [ -f "$tree/$sub/$pkg/__init__.py" ] || \
                adk_nix_die "launcher: $name has no $sub/$pkg/__init__.py at $tree"
        done
        realpath -- "$tree/$sub" || adk_nix_die "launcher: cannot resolve $tree/$sub"
    done <<< "$ADK_NIX_LAUNCHER_TREES"
}

adk_nix_build_main() {
    local var roots=() found text dir sha key
    for var in ADK_NIX_LIBRELANE ADK_NIX_SG13G2_PDK ADK_NIX_TREES ADK_NIX_STATE ADK_NIX_MODE; do
        [ -n "${!var:-}" ] || adk_nix_die "$var is not set; enter through devShells.build"
    done
    if [ "$ADK_NIX_MODE" = evidence ]; then
        [ "$ADK_NIX_STORE_DIR" = /nix/store ] || adk_nix_die "evidence: ADK_NIX_STORE_DIR must be /nix/store"
        for var in ADK_NIX_VENV ADK_NIX_LIB ADK_NIX_CONTEXT_OUT; do
            [ -n "${!var:-}" ] || adk_nix_die "evidence: $var is not set"
        done
    fi
    adk_nix_store_path "$ADK_NIX_SG13G2_PDK" || adk_nix_die "$ADK_NIX_SG13G2_PDK is not a store path"
    adk_nix_die_build_env "$ADK_NIX_SG13G2_PDK" || \
        adk_nix_die "no ihp-sg13g2/libs.tech/librelane under $ADK_NIX_SG13G2_PDK"

    # Taken whole before it is split: a process substitution would drop the
    # exit of a tree refused after an earlier one was printed.
    found="$(adk_nix_launcher_roots "$ADK_NIX_TREES")" || exit 1
    [ -n "$found" ] || adk_nix_die "launcher: no source roots"
    mapfile -t roots <<< "$found"
    text="$(adk_nix_launcher_text "$ADK_NIX_LIBRELANE" "${roots[@]}")" || exit 1
    # One directory per launcher text, so two shells over different trees
    # never rewrite each other's launcher under a run that recorded it.
    key="$(printf '%s\n' "$text" | sha256sum | cut -c1-16)"
    [[ "$key" =~ ^[0-9a-f]{16}$ ]] || adk_nix_die "launcher: cannot key it"
    dir="$ADK_NIX_STATE/librelane-launcher-$key"
    sha="$(adk_nix_write_launcher "$dir" "$ADK_NIX_LIBRELANE" "${roots[@]}")" || exit 1
    export CHIPLET_FLOW_LIBRELANE_LAUNCHER="$dir/librelane"
    export CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256="$sha"
    if [ "$ADK_NIX_MODE" = evidence ]; then
        # The CLI hook left the shell in the context directory, beside
        # nix-context.json. This asks the launcher itself what it imports.
        # -I -S: its own interpreter reads no PYTHON* variable and no site
        # directory (so no sitecustomize and no NIX_PYTHONPATH); the probe
        # still hands the launcher this shell's environment unchanged.
        "$ADK_NIX_VENV/bin/python" -I -S "$ADK_NIX_LIB/capture_build_context.py" \
            "$ADK_NIX_CONTEXT_OUT/build-context.json" \
            || adk_nix_die "build context capture failed; nothing was run"
    fi
    adk_nix_note "build shell ready: PDK_ROOT $PDK_ROOT, LibreLane $ADK_NIX_LIBRELANE, launcher $CHIPLET_FLOW_LIBRELANE_LAUNCHER"
}

if [ -z "${ADK_NIX_LIB_ONLY:-}" ]; then
    adk_nix_build_main
fi
