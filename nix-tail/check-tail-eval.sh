#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The tail shell's evaluation check: what devShells.tail adds, and that the
# cli and build shells are unchanged by it, read from the derivations Nix
# evaluates, with no derivation built (--max-jobs 0) and nothing fetched
# (--offline; the inputs must already be in the store).
#
# It evaluates two clean adk-tools checkouts, a base without the tail shell
# (or with it; only the cli and build shells and two packages are read there)
# and this one, and nix-tail/tail_eval.py compares them. Exit 0 only when:
#
#   - the cli and build shells differ from the base's only in the revision
#     they record and the output path that follows from it;
#   - devShells.tail is the cli shell plus tail-wrappers, tail-export-python,
#     kicad-fork-base and sg13g2-pdk, and nothing else (tail_eval.py lists
#     what that means field by field);
#   - kicad-fork-base and sg13g2-pdk are the base's derivations, so the tail
#     shell builds neither again where the base's are built;
#   - the tail-wrappers derivation the tail shell takes, whatever form the
#     flake binds it in, has the build command and variables the one binding
#     nix-tail/check-tail-wrappers.sh reads gives it, with store paths in the
#     places that binding fills, and the wrappers it renders from the
#     templates it names are the image's commands with only store paths and
#     the venv and tools/ roots in them. Its templates and the carrier DRC
#     script are read from the store (ADK_NIX_STORE_DIR, default /nix/store,
#     is for the tests; --keep records which), where the evaluation put them.
#
# The bash that fills @bash@ is checked by shape only (one store path ending
# /bin/bash), so neither this check nor check-tail-wrappers.sh catches one
# that drops -p, such as a shim that runs bash without it. What
# tail-export-python and the nix-tail/ store copy hold is not read either.
#
# Usage: nix-tail/check-tail-eval.sh [--keep <dir>] <base checkout> [<checkout, default this one>]
# --keep writes what Nix printed, the store files, the store they were read
# from and what was compared into <dir>, which must not exist yet; without it they go to a temporary
# directory, removed at exit.

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/../nix/cli-env.sh"

keep=""
base=""
at=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep) keep="${2:-}"; shift 2 || adk_nix_die "tail eval: --keep needs a directory" ;;
        -*) adk_nix_die "tail eval: unknown option $1" ;;
        *)
            if [ -z "$base" ]; then base="$1"
            elif [ -z "$at" ]; then at="$1"
            else adk_nix_die "tail eval: a base and one checkout only"
            fi
            shift ;;
    esac
done
[ -n "$base" ] || adk_nix_die "tail eval: the base checkout is required"
at="${at:-$(cd "$here/.." && pwd)}"
command -v python3 >/dev/null || adk_nix_die "tail eval: python3 is needed"
system="${ADK_NIX_SYSTEM:-x86_64-linux}"
store="${ADK_NIX_STORE_DIR:-/nix/store}"

# HEAD of a clean checkout: the revision the flake records as
# ADK_NIX_FLAKE_REV. A dirty tree records none, so it is refused.
rev_of() {
    local dir="$1" rev
    rev="$(adk_nix_git -C "$dir" rev-parse --verify --quiet HEAD)" || \
        adk_nix_die "tail eval: $dir is not a git checkout with a HEAD"
    [ -z "$(adk_nix_git -C "$dir" status --porcelain --untracked-files=normal)" ] || \
        adk_nix_die "tail eval: $dir has changes; its flake would record no revision"
    echo "$rev"
}
base_rev="$(rev_of "$base")" || exit 1
new_rev="$(rev_of "$at")" || exit 1

if [ -n "$keep" ]; then
    [ ! -e "$keep" ] || adk_nix_die "tail eval: $keep exists"
    mkdir -p "$keep" || adk_nix_die "tail eval: cannot make $keep"
    work="$keep"
else
    work="$(mktemp -d "${TMPDIR:-/tmp}/adk-tail-eval.XXXXXX")" || adk_nix_die "tail eval: mktemp failed"
    trap 'rm -rf "$work"' EXIT
fi
printf '{"base": "%s", "new": "%s"}\n' "$base_rev" "$new_rev" > "$work/revs.json"
printf '%s\n' "$store" > "$work/store-dir"

nixo=(--offline --no-update-lock-file --max-jobs 0 --option allow-import-from-derivation false)
# (names: set: ...) applied to the names: a function of the flake's attribute
# set that forces the drvPath of those names only.
drv_paths='names: set: builtins.mapAttrs (n: d: d.drvPath) (builtins.intersectAttrs names set)'
shells="($drv_paths) { cli = 0; build = 0; tail = 0; }"
packages="($drv_paths) { kicad-fork-base = 0; sg13g2-pdk = 0; tail-wrappers = 0; tail-export-python = 0; }"

for side in base new; do
    case "$side" in base) dir="$base" ;; *) dir="$at" ;; esac
    nix eval --json "${nixo[@]}" "$dir#devShells.$system" --apply "$shells" > "$work/$side.shells.json" || \
        adk_nix_die "tail eval: evaluating the $side shells failed"
    nix eval --json "${nixo[@]}" "$dir#packages.$system" --apply "$packages" > "$work/$side.packages.json" || \
        adk_nix_die "tail eval: evaluating the $side packages failed"
    python3 -I -S -c '
import json, sys
shells, packages, out = sys.argv[1:4]
attrs = {"shells": json.load(open(shells)), "packages": json.load(open(packages))}
with open(out, "x") as handle:
    json.dump(attrs, handle, indent=1, sort_keys=True)
' "$work/$side.shells.json" "$work/$side.packages.json" "$work/$side.attrs.json" || \
        adk_nix_die "tail eval: what the $side evaluation printed is not JSON"
    drvs="$(python3 -I -S "$here/tail_eval.py" drvs "$work/$side.attrs.json")" || exit 1
    # shellcheck disable=SC2086
    nix derivation show $drvs > "$work/$side.show.json" || \
        adk_nix_die "tail eval: nix derivation show of the $side derivations failed"
    drvs="$(python3 -I -S "$here/tail_eval.py" closure-infos "$work/$side.show.json")" || exit 1
    # shellcheck disable=SC2086
    nix derivation show $drvs > "$work/$side.show-ci.json" || \
        adk_nix_die "tail eval: nix derivation show of the $side closure-info derivations failed"
done

# The files the tail-wrappers build command names among its sources (the
# templates and the carrier DRC script), copied out of the store by store
# name, and this checkout's bin/adk-verify-carrier-drc: the reader renders the
# wrappers from these bytes. What is not a plain file is not copied, and the
# reader finds it missing.
srcs="$(python3 -I -S "$here/tail_eval.py" wrapper-sources "$work/new.attrs.json" "$work/new.show.json")" || exit 1
mkdir "$work/new.sources" || adk_nix_die "tail eval: cannot make $work/new.sources"
for src in $srcs; do
    file="$store/${src#/nix/store/}"
    if [ -f "$file" ] && [ ! -L "$file" ]; then
        cp -- "$file" "$work/new.sources/${src#/nix/store/}" || adk_nix_die "tail eval: cannot copy $file"
    fi
done
adk_nix_git -C "$at" show HEAD:bin/adk-verify-carrier-drc > "$work/new.carrier-drc" || \
    adk_nix_die "tail eval: bin/adk-verify-carrier-drc is not in HEAD of $at"

python3 -I -S "$here/tail_eval.py" compare "$work"
