# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Sourced by the shellHook of devShells.cli (flake.nix); not run on its own.
#
# Turns the Nix environment into one packaging-flow can run in up to the signoff
# gate of a ready-die spec, and records nothing it did not check. Two modes:
#
#   dev (default)  the caller names the source trees and the three roots. They
#                  are used as they are, edits and all; a run records
#                  `<sha>-dirty` exactly as it does anywhere else. Nothing here
#                  makes such a run qualified.
#   evidence       ADK_NIX_MODE=evidence. Every tree comes from this adk-tools
#                  checkout at the commit this flake was evaluated from: the
#                  flow's packages from staging/ (stage-sources.sh), at their
#                  staging.pin refs; the roots from the tools/ submodules, at
#                  their gitlinks. Anything else refuses, before the install
#                  and again after it, and so does anything that could hide a
#                  change from git. The venv is new every time, and held by
#                  this shell for its life. The context record is written
#                  before the command runs, and the shell refuses if it finds a
#                  module imported from anywhere but the realised closure or its
#                  verified tree.
#
# Both modes write the tool identity table packaging-flow keys a run's tools by,
# from this shell's closure (adk_nix_tool_table), and export it as
# ADK_TOOLS_TABLE with its closure digest as ADK_TOOLS_CLOSURE.
#
# What it does not give: network isolation, a CPU or memory cap, a separate
# user, a timeout. `nix develop` is not the launcher. See docs/NIX.md.
#
# Inputs from flake.nix: ADK_NIX_PYTHON, ADK_NIX_SITE_PACKAGES, ADK_NIX_OPENROAD,
# ADK_NIX_KLAYOUT, ADK_NIX_KLAYOUT_IMAGE_VERSION, ADK_NIX_CLOSURE_INFO,
# ADK_NIX_FLAKE_REV, ADK_NIX_FLAKE_LOCK, ADK_NIX_LIB, ADK_NIX_COSIM_DEVSHELL,
# ADK_NIX_STAGING_PIN (the flake's staging.pin, as a store copy).
# Inputs from the caller:
#   ADK_NIX_MODE          dev | evidence (default dev)
#   ADK_NIX_STATE         where the venvs live (default $XDG_STATE_HOME or
#                         ~/.local/state, /adk-tools-nix; required in evidence
#                         mode, which runs without a HOME)
#   dev:      ADK_NIX_SOURCES (a directory holding chiplet-flow, packaging-flow,
#             interposer-pnr, chiplet-spec, assembly-lvs, chiplet-cosim)
#             and/or one ADK_NIX_SRC_<NAME> per tree
#             (ADK_NIX_SRC_CHIPLET_FLOW, ...); ADK_ROOT,
#             INTERPOSER_PDK_ROOT, INTERCONNECT_PDK_ROOT.
#   evidence: ADK_NIX_ADK_TOOLS (the clean adk-tools checkout this flake is),
#             ADK_NIX_CONTEXT_OUT (an absolute path to a new or empty
#             directory; nix-context.json is written there, and the command
#             runs from there).

# name|staging.pin ref variable|directory inside the tree that pip installs.
# One row per directory pip installs: a tree installed from two has two rows,
# which are one checkout at one pin, so they name the same ref variable.
# chiplet-spec is such a tree: its reader installs from reference/python and
# the system description, which packaging-flow depends on, from description
# (see the Dockerfile's install step). It is also used as a whole tree: the
# canonical schemas sit at its root.
# assembly-lvs is the engine lvs-verify runs, found as `assembly-lvs` on PATH:
# its console script lands in the venv's bin, which the shell puts first.
# chiplet-cosim is the co-simulation tool; its console script is the file the
# tool table keys it by.
ADK_NIX_EDITABLE="
chiplet-flow|STAGE_CHIPLET_FLOW_REF|.
packaging-flow|STAGE_PACKAGING_FLOW_REF|.
interposer-pnr|STAGE_INTERPOSER_PNR_REF|.
chiplet-spec|STAGE_CHIPLET_SPEC_REF|reference/python
chiplet-spec|STAGE_CHIPLET_SPEC_REF|description
assembly-lvs|STAGE_ASSEMBLY_LVS_REF|.
chiplet-cosim|STAGE_CHIPLET_COSIM_REF|.
"

# The store every path this shell checks must sit in. Set here, never read from
# the caller.
ADK_NIX_STORE=/nix/store

# Root variable (the names the image exports and chiplet-flow's roots.py
# reads) | submodule path in adk-tools that pins it.
ADK_NIX_ROOTS="
ADK_ROOT|tools/adk
INTERPOSER_PDK_ROOT|tools/OpenIntM4TM2
INTERCONNECT_PDK_ROOT|tools/IHP-Interconnect-IntM4TM2
"

# Host variables that would otherwise decide something silently: an imported
# module, a technology file, a root found by walking up from a checkout. The
# roots this shell sets are cleared too, and set again from what was declared.
# KLayout reads every KLAYOUT_ name below (its libraries carry them), and
# LD_LIBRARY_PATH, RUBYOPT and RUBYLIB steer `klayout -b` ahead of the Nix
# RUNPATH and its own Ruby. packaging-flow reads the ADK_TOOLS_ names for the
# environment a run is in and chiplet-cosim reads COSIM_DEVSHELL; this shell
# sets the ones it declares.
ADK_NIX_SCRUB="
PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV CONDA_PREFIX
PDK_ROOT PDK KLAYOUT_PATH KLAYOUT_HOME KLAYOUT_PYTHONPATH KLAYOUT_PYTHONHOME
KLAYOUT_IGNORE_MACRO_DIRS KLAYOUT_SALT_MINE KLAYOUT_VERBOSITY
LD_LIBRARY_PATH RUBYOPT RUBYLIB
HCDP_ROOT ADK_TOOLS_ROOT GDS_TO_KICAD_ROOT CHIPLET_KICAD_PLUGIN_ROOT
CHIPLET_SPEC_ROOT KICAD_CHIPLET_PYTHON ADK_PYTHON ADK_REGISTRY_DIR
CHIPLET_FLOW_ENGINE_PYTHON CHIPLET_FLOW_PADMAP_PYTHON CHIPLET_FLOW_ASSEMBLY_LVS
OPENROAD
ADK_TOOLS_TABLE ADK_TOOLS_CLOSURE ADK_TOOLS_IMAGE COSIM_DEVSHELL
GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
GIT_ATTR_SOURCE GIT_CEILING_DIRECTORIES GIT_NAMESPACE GIT_REPLACE_REF_BASE
"

adk_nix_die() {
    echo "adk-nix: $*" >&2
    exit 1
}

adk_nix_note() {
    echo "adk-nix: $*" >&2
}

# ADK_NIX_SRC_ variable for a tree name: chiplet-flow -> ADK_NIX_SRC_CHIPLET_FLOW
adk_nix_src_var() {
    local up="${1//-/_}"
    echo "ADK_NIX_SRC_${up^^}"
}

# "<commit> clean|dirty" for a git checkout, or nothing (status 1) if it is not
# one. Asked the way chiplet-flow's provenance asks it, so the two agree.
adk_nix_checkout_state() {
    local dir="$1" commit porcelain
    commit="$(git -C "$dir" rev-parse HEAD 2>/dev/null)" || return 1
    porcelain="$(git -C "$dir" status --porcelain 2>/dev/null)" || return 1
    if [ -z "$porcelain" ]; then
        echo "$commit clean"
    else
        echo "$commit dirty"
    fi
}

# git as the evidence checks ask it. The caller's git environment is dropped
# (GIT_DIR and its kind override what -C finds, GIT_CONFIG_* add settings), no
# global or system configuration is read and no replace objects are used. The
# repository's own settings that could make a change invisible are overridden
# on the command line, which wins over the repository's config: an fsmonitor
# answering for the tree, an untracked cache, an extra excludes or attributes
# file, untracked files not shown, a coarse stat comparison.
adk_nix_git() {
    env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR \
        -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES \
        -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT -u GIT_ATTR_SOURCE \
        -u GIT_CEILING_DIRECTORIES -u GIT_NAMESPACE -u GIT_REPLACE_REF_BASE \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
        git -c core.fsmonitor=false -c core.untrackedCache=false \
            -c core.excludesFile=/dev/null -c core.attributesFile=/dev/null \
            -c status.showUntrackedFiles=all -c core.checkStat=default \
            -c core.trustctime=true -c core.ignoreStat=false "$@"
}

# Ignored, untracked paths in $dir, one per line, ignored directories listed
# once. With install directories (space-separated), the .egg-info an editable
# install may leave in each (or in its src/) is left out; without one, nothing
# is.
adk_nix_ignored() {
    local dir="$1" install="${2:-}"
    adk_nix_git -C "$dir" ls-files -z --others --ignored --exclude-standard --directory \
        | tr '\0' '\n' \
        | awk -v dirs="$install" '
            BEGIN {
                n = split(dirs, d, " ")
                for (i = 1; i <= n; i++) {
                    sub(/\/$/, "", d[i])
                    p[i] = d[i] == "." ? "" : d[i] "/"
                }
            }
            {
                for (i = 1; i <= n; i++) {
                    if (substr($0, 1, length(p[i])) != p[i]) continue
                    rest = substr($0, length(p[i]) + 1)
                    if (rest ~ /^[^\/]+\.egg-info\/$/ || rest ~ /^src\/[^\/]+\.egg-info\/$/) next
                }
                print
            }'
}

# Refuse unless $dir is a clean checkout at exactly $want. $what names it in
# the message. $kind says what the directory is:
#   checkout        adk-tools itself. Its submodules are left to the roots'
#                   own checks against the gitlinks in HEAD, and its ignored
#                   files (staging/, result) are expected: nothing in it is on
#                   the run's import path, which starts in the context
#                   directory, not here.
#   root            a root (default): nothing ignored may exist in it.
#   source:<dirs>   a tree installed editable from <dirs> (one directory, or
#                   several separated by spaces): the only ignored files
#                   allowed are the .egg-info the install may leave in each.
#
# Clean means what git says with nothing able to hide a change from it. An
# index entry marked assume-unchanged or skip-worktree is a tracked file
# status does not look at; a content filter from the repository's own config
# can make a changed file hash as unchanged; core.worktree can point git at
# another directory; an ignore rule (in .git/info/exclude, which no option
# turns off, or in an untracked .gitignore) is an untracked file status does
# not list, and Python still imports an ignored __pycache__ or .so. Each
# refuses, by name, rather than being worked around.
adk_nix_require_pinned() {
    local what="$1" dir="$2" want="$3" kind="${4:-root}" commit top flagged filters
    local submodules=none install="" porcelain ignored
    case "$kind" in
        checkout) submodules=all ;;
        root) ;;
        source:?*) install="${kind#source:}" ;;
        *) adk_nix_die "$what: unknown checkout kind $kind" ;;
    esac
    [ -n "$want" ] || adk_nix_die "$what: no pin to compare against"
    commit="$(adk_nix_git -C "$dir" rev-parse HEAD 2>/dev/null)" || \
        adk_nix_die "$what: $dir is not a git checkout"
    [ "$commit" = "$want" ] || \
        adk_nix_die "$what: $dir is at $commit, pinned $want"
    top="$(adk_nix_git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" || \
        adk_nix_die "$what: $dir has no work tree"
    [ "$top" = "$(realpath "$dir")" ] || \
        adk_nix_die "$what: git's work tree for $dir is $top (core.worktree, or no repository of its own)"
    # ls-files -v tags a normal entry H; S is skip-worktree, lower case is
    # assume-unchanged.
    flagged="$(adk_nix_git -C "$dir" ls-files -v | awk '$1 != "H" { n++ } END { print n + 0 }')"
    [ "$flagged" -eq 0 ] || \
        adk_nix_die "$what: $dir has $flagged index entries marked assume-unchanged or skip-worktree"
    # All of the repository's configuration, includes and worktree config
    # with it (no global or system file is read here).
    filters="$(adk_nix_git -C "$dir" config --name-only --get-regexp '^filter\.' 2>/dev/null || true)"
    [ -z "$filters" ] || \
        adk_nix_die "$what: $dir configures content filters ($(paste -sd' ' - <<< "$filters")); a filtered file can hash as unchanged"
    porcelain="$(adk_nix_git -C "$dir" status --porcelain --untracked-files=all \
        --ignore-submodules="$submodules")" || adk_nix_die "$what: git status failed in $dir"
    [ -z "$porcelain" ] || \
        adk_nix_die "$what: $dir has uncommitted changes"
    if [ "$kind" != checkout ]; then
        ignored="$(adk_nix_ignored "$dir" "$install")"
        [ -z "$ignored" ] || \
            adk_nix_die "$what: $dir holds ignored files: $(head -n 3 <<< "$ignored" | paste -sd' ' -)"
    fi
}

# The value of a variable as staging.pin sets it, read in a fresh bash under
# `set -euo pipefail`, the way stage-sources.sh sources the file, so the pin
# file cannot change this shell. A fresh process because a subshell called as
# `value="$(...)" || ...` runs with errexit suppressed, and the status of `.`
# alone is the file's last command's. The status is non-zero when the file
# does not source cleanly (a failing command anywhere, a failed substitution,
# an unset reference, a syntax error), so a caller that checks it can refuse
# such a file; the value is printed either way, as far as the file got.
adk_nix_pin() {
    "${BASH:-bash}" -euo pipefail -c '
        readonly adk_nix_pin_var="$2"
        trap '\''printf "%s" "${!adk_nix_pin_var:-}"'\'' EXIT
        . "$1" >/dev/null 2>&1' adk_nix_pin "$1" "$2"
}

# The commit a submodule path is pinned at in HEAD of the adk-tools checkout.
adk_nix_gitlink() {
    local adk_tools="$1" path="$2"
    adk_nix_git -C "$adk_tools" ls-tree HEAD "$path" 2>/dev/null | awk '$2 == "commit" { print $3 }'
}

# The resolved path of a file this shell provides, refused unless the store path
# it sits in is listed in this shell's closure: $ADK_NIX_CLOSURE_INFO/store-paths,
# read as a file, never a store query (evidence mode has no nix to ask). $what
# names it in the message.
adk_nix_in_closure() {
    local what="$1" path="$2" real rest top
    real="$(realpath -e -- "$path" 2>/dev/null)" || adk_nix_die "$what: $path does not resolve"
    rest="${real#"$ADK_NIX_STORE"/}"
    top="${rest%%/*}"
    [ "$rest" != "$real" ] && [[ "$top" =~ ^[0-9a-z]{32}-.+$ ]] || \
        adk_nix_die "$what: $path resolves to $real, which is not in $ADK_NIX_STORE"
    [ -f "$ADK_NIX_CLOSURE_INFO/store-paths" ] || \
        adk_nix_die "$what: $ADK_NIX_CLOSURE_INFO has no store-paths"
    grep -Fxq -- "$ADK_NIX_STORE/$top" "$ADK_NIX_CLOSURE_INFO/store-paths" || \
        adk_nix_die "$what: $ADK_NIX_STORE/$top is not in this shell's closure"
    echo "$real"
}

# One line per row of ADK_NIX_EDITABLE: "<name>|<tree>|<install dir>". The rows
# of one tree resolve to the same tree. Resolved, not yet checked.
adk_nix_trees() {
    local name refvar sub tree var
    while IFS='|' read -r name refvar sub; do
        [ -n "$name" ] || continue
        if [ "$ADK_NIX_MODE" = evidence ]; then
            tree="$ADK_NIX_ADK_TOOLS/staging/$name"
        else
            var="$(adk_nix_src_var "$name")"
            tree="${!var:-}"
            if [ -z "$tree" ] && [ -n "${ADK_NIX_SOURCES:-}" ]; then
                tree="$ADK_NIX_SOURCES/$name"
                [ -d "$tree" ] || \
                    adk_nix_die "$name: $tree does not exist; put $name in ADK_NIX_SOURCES or set $var"
            fi
            [ -n "$tree" ] || adk_nix_die "$name: set $var or ADK_NIX_SOURCES (a directory holding $name)"
        fi
        echo "$name|$tree|$sub"
    done <<< "$ADK_NIX_EDITABLE"
}

# One line per tree of ADK_NIX_EDITABLE, in the table's order: "<name>|<ref
# variable>|<install dirs>", the directories of its rows separated by spaces.
# Refuses (status 1) rows of one tree that name different ref variables: they
# are one checkout, which has one pin.
adk_nix_editable_trees() {
    awk -F'|' '
        NF == 0 { next }
        !($1 in ref) { order[++n] = $1; ref[$1] = $2; dirs[$1] = $3; next }
        ref[$1] != $2 {
            printf "adk-nix: %s: its rows name both %s and %s\n", $1, ref[$1], $2 > "/dev/stderr"
            bad = 1
            exit 1
        }
        { dirs[$1] = dirs[$1] " " $3 }
        END {
            if (bad) exit 1
            for (i = 1; i <= n; i++) print order[i] "|" ref[order[i]] "|" dirs[order[i]]
        }' <<< "$ADK_NIX_EDITABLE"
}

# Evidence mode: the adk-tools checkout, every staged tree and every root must
# be exactly what this commit pins. Called before and after the install. A tree
# installed from two directories is checked once, allowing the .egg-info of
# both.
adk_nix_check_evidence() {
    local pinfile name tree sub refvar var path want editable
    [[ "$ADK_NIX_FLAKE_REV" =~ ^[0-9a-f]{40}$ ]] || \
        adk_nix_die "evidence: the flake was evaluated from a dirty or non-git tree ($ADK_NIX_FLAKE_REV)"
    [ -n "${ADK_NIX_ADK_TOOLS:-}" ] || adk_nix_die "evidence: set ADK_NIX_ADK_TOOLS"
    adk_nix_require_pinned "adk-tools" "$ADK_NIX_ADK_TOOLS" "$ADK_NIX_FLAKE_REV" checkout
    pinfile="$ADK_NIX_ADK_TOOLS/staging.pin"
    [ -f "$pinfile" ] || adk_nix_die "evidence: $pinfile not found"
    editable="$(adk_nix_editable_trees)" || exit 1
    while IFS='|' read -r name refvar sub; do
        [ -n "$name" ] || continue
        want="$(adk_nix_pin "$pinfile" "$refvar")"
        adk_nix_require_pinned "$name" "$ADK_NIX_ADK_TOOLS/staging/$name" "$want" "source:$sub"
    done <<< "$editable"
    while IFS='|' read -r var path; do
        [ -n "$var" ] || continue
        # From HEAD, which is the flake's revision: a gitlink moved in the
        # index or the tree does not move the pin.
        want="$(adk_nix_gitlink "$ADK_NIX_ADK_TOOLS" "$path")"
        adk_nix_require_pinned "$var ($path)" "$ADK_NIX_ADK_TOOLS/$path" "$want" root
    done <<< "$ADK_NIX_ROOTS"
}

adk_nix_main() {
    local line name tree sub var ref path venv key stamp current pipargs=()

    ADK_NIX_MODE="${ADK_NIX_MODE:-dev}"
    case "$ADK_NIX_MODE" in
        dev|evidence) ;;
        *) adk_nix_die "ADK_NIX_MODE must be dev or evidence, not $ADK_NIX_MODE" ;;
    esac
    for var in ADK_NIX_PYTHON ADK_NIX_SITE_PACKAGES ADK_NIX_OPENROAD ADK_NIX_KLAYOUT \
               ADK_NIX_KLAYOUT_IMAGE_VERSION ADK_NIX_CLOSURE_INFO \
               ADK_NIX_FLAKE_REV ADK_NIX_FLAKE_LOCK ADK_NIX_LIB \
               ADK_NIX_COSIM_DEVSHELL ADK_NIX_STAGING_PIN; do
        [ -n "${!var:-}" ] || adk_nix_die "$var is not set; enter through devShells.cli"
    done
    # Every tree this shell installs has its ref in staging.pin, one ref for
    # all of a tree's rows. A row whose ref is gone means the table has not
    # followed the pin.
    adk_nix_editable_trees >/dev/null || exit 1
    while IFS='|' read -r name var sub; do
        [ -n "$name" ] || continue
        ref="$(adk_nix_pin "$ADK_NIX_STAGING_PIN" "$var")" || \
            adk_nix_die "$ADK_NIX_STAGING_PIN does not source cleanly"
        [ -n "$ref" ] || \
            adk_nix_die "staging.pin has no $var for $name: ADK_NIX_EDITABLE has not followed the pin"
    done <<< "$ADK_NIX_EDITABLE"
    adk_nix_check_pinned_paths

    if [ -z "${ADK_NIX_STATE:-}" ]; then
        [ "$ADK_NIX_MODE" = dev ] || adk_nix_die "evidence: set ADK_NIX_STATE"
        ADK_NIX_STATE="${XDG_STATE_HOME:-$HOME/.local/state}/adk-tools-nix"
    fi
    # Resolved before anything is keyed or written under it: the venv's path is
    # in its console scripts and in a run's identity, so one directory reached by
    # two spellings is still one venv.
    mkdir -p -- "$ADK_NIX_STATE" || adk_nix_die "cannot create $ADK_NIX_STATE"
    ADK_NIX_STATE="$(realpath -e -- "$ADK_NIX_STATE")" || adk_nix_die "cannot resolve ADK_NIX_STATE"

    if [ "$ADK_NIX_MODE" = evidence ]; then
        [ -n "${ADK_NIX_CONTEXT_OUT:-}" ] || adk_nix_die "evidence: set ADK_NIX_CONTEXT_OUT"
        case "$ADK_NIX_CONTEXT_OUT" in
            /*) ;;
            *) adk_nix_die "evidence: ADK_NIX_CONTEXT_OUT must be an absolute path" ;;
        esac
        if [ -e "$ADK_NIX_CONTEXT_OUT" ]; then
            [ -d "$ADK_NIX_CONTEXT_OUT" ] && [ -z "$(ls -A "$ADK_NIX_CONTEXT_OUT")" ] || \
                adk_nix_die "evidence: $ADK_NIX_CONTEXT_OUT is not a new or empty directory"
        fi
        adk_nix_check_evidence
        for line in $ADK_NIX_ROOTS; do
            var="${line%%|*}"
            path="${line#*|}"
            export "$var=$ADK_NIX_ADK_TOOLS/$path"
        done
    else
        for line in $ADK_NIX_ROOTS; do
            var="${line%%|*}"
            [ -n "${!var:-}" ] || adk_nix_die "dev: set $var (no fallback is guessed)"
            [ -d "${!var}" ] || adk_nix_die "dev: $var=${!var} is not a directory"
        done
    fi

    # Resolve the trees before the scrub clears anything they were named by.
    local trees
    trees="$(adk_nix_trees)" || exit 1
    while IFS='|' read -r name tree sub; do
        [ -d "$tree/$sub" ] || adk_nix_die "$name: $tree/$sub is not a directory"
        adk_nix_checkout_state "$tree" >/dev/null || \
            adk_nix_note "$name: $tree is not a git checkout; its identity will be recorded unavailable"
    done <<< "$trees"
    local spec_tree
    # Its rows name one tree (adk_nix_trees); the first says which.
    spec_tree="$(awk -F'|' '$1 == "chiplet-spec" { print $2; exit }' <<< "$trees")"
    local roots_saved=()
    for line in $ADK_NIX_ROOTS; do
        var="${line%%|*}"
        roots_saved+=("$var=$(realpath "${!var}")")
    done

    local v
    for v in $ADK_NIX_SCRUB; do
        unset "$v"
    done
    for v in "${roots_saved[@]}"; do
        export "$v"
    done

    # Fresh, empty config homes: nothing from ~/.klayout (the host registers a
    # second SG13_dev there, and `klayout -b` loads every technology in a
    # home's tech/) or ~/.config/adk-tools reaches the run. Evidence mode has
    # no HOME at all under --ignore-environment, so it gets one too.
    ADK_NIX_TMP="$(mktemp -d "${TMPDIR:-/tmp}/adk-nix.XXXXXX")" || adk_nix_die "mktemp failed"
    # mktemp keeps a TMPDIR's trailing slash, which gives a double slash here
    # and in every path recorded under it; the resolved path has none.
    ADK_NIX_TMP="$(realpath -e -- "$ADK_NIX_TMP")" || adk_nix_die "cannot resolve the entry's directory"
    mkdir -p "$ADK_NIX_TMP/klayout" "$ADK_NIX_TMP/config"
    export KLAYOUT_HOME="$ADK_NIX_TMP/klayout"
    export XDG_CONFIG_HOME="$ADK_NIX_TMP/config"
    # The image's search path, in the image's shape: the writable home first,
    # then the interposer PDK's KLayout tree, which registers its technology.
    # From the root this shell verified or was given, never a guessed checkout.
    [ -d "$INTERPOSER_PDK_ROOT/libs.tech/klayout" ] || \
        adk_nix_die "INTERPOSER_PDK_ROOT=$INTERPOSER_PDK_ROOT has no libs.tech/klayout for KLAYOUT_PATH"
    export KLAYOUT_PATH="$KLAYOUT_HOME:$INTERPOSER_PDK_ROOT/libs.tech/klayout"
    if [ "$ADK_NIX_MODE" = evidence ] || [ -z "${HOME:-}" ]; then
        mkdir -p "$ADK_NIX_TMP/home"
        export HOME="$ADK_NIX_TMP/home"
    fi
    export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
    export PIP_NO_INDEX=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
    export QT_QPA_PLATFORM=offscreen
    export CHIPLET_FLOW_THREAD_CAP="${CHIPLET_FLOW_THREAD_CAP:-2}"

    # The venv is made from the bare interpreter, not from the withPackages
    # wrapper: the wrapper points sys.executable back at itself, and chiplet-flow
    # starts its engine with sys.executable when nothing else names one. The
    # third-party packages come in through one .pth, from the Nix environment.
    venv="$(adk_nix_venv_dir "$trees")" || exit 1
    [ "$ADK_NIX_MODE" = dev ] || adk_nix_venv_claim "$venv"
    if [ ! -x "$venv/bin/python" ]; then
        "$ADK_NIX_PYTHON" -m venv --without-pip "$venv" || adk_nix_die "venv creation failed at $venv"
        local site
        site="$("$venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" || \
            adk_nix_die "cannot locate the venv's site-packages"
        printf 'import site; site.addsitedir(%s)\n' "'$ADK_NIX_SITE_PACKAGES'" \
            > "$site/_adk_nix_environment.pth"
    fi

    stamp="$(adk_nix_stamp_from "$trees")"
    current="$(cat "$venv/.adk-nix-stamp" 2>/dev/null || true)"
    if [ "$ADK_NIX_MODE" = evidence ] || [ "$stamp" != "$current" ]; then
        while IFS='|' read -r name tree sub; do
            pipargs+=(-e "$tree/$sub")
        done <<< "$trees"
        # Only the flow's own packages, and none of their dependencies: every
        # third-party package comes from Nix, and pip check below proves the
        # declared requirements are met by what Nix provides. pip's output goes
        # to stderr: stdout belongs to the command the shell was entered for.
        "$venv/bin/python" -m pip install --quiet --no-index --no-deps \
            --no-build-isolation "${pipargs[@]}" >&2 || adk_nix_die "editable install failed"
        "$venv/bin/python" -m pip check >&2 || adk_nix_die "pip check failed in $venv"
        echo "$stamp" > "$venv/.adk-nix-stamp"
    fi

    export VIRTUAL_ENV="$venv"
    export PATH="$venv/bin:$PATH"
    export CHIPLET_FLOW_ENGINE_PYTHON="$venv/bin/python"
    # The flow's klayout.db interpreter, named rather than found: unset, the
    # flow would take a gds_to_kicad venv/ first if one sat beside the trees.
    export CHIPLET_FLOW_PADMAP_PYTHON="$venv/bin/python"
    export OPENROAD="$ADK_NIX_OPENROAD/bin/openroad"
    [ -x "$OPENROAD" ] || adk_nix_die "no openroad at $OPENROAD"
    [ -x "$ADK_NIX_KLAYOUT/bin/klayout" ] || adk_nix_die "no klayout at $ADK_NIX_KLAYOUT/bin/klayout"
    export CHIPLET_SPEC_ROOT="$spec_tree"
    # Checked against its pin and this shell's closure before the scrub.
    export COSIM_DEVSHELL="$ADK_NIX_COSIM_DEVSHELL"
    # The trees as resolved, one "<name>|<tree>|<install dir>" per row of the
    # table: the context capture checks every module of ours was imported from
    # its tree.
    export ADK_NIX_TREES="$trees"
    export ADK_NIX_VENV="$venv" ADK_NIX_MODE ADK_NIX_STATE ADK_NIX_TMP
    export ADK_NIX_KLAYOUT ADK_NIX_KLAYOUT_IMAGE_VERSION

    if [ "$ADK_NIX_MODE" = evidence ]; then
        # The install writes only ignored files (egg-info); prove it.
        adk_nix_check_evidence
    fi
    adk_nix_tool_table "$venv" "$trees"
    if [ "$ADK_NIX_MODE" = evidence ]; then
        # The command runs from here. `python -m` puts its working directory
        # first on the import path, ahead of everything checked above, so it
        # is the context directory, which was new or empty, and the capture
        # checks it still holds nothing before writing into it.
        mkdir -p "$ADK_NIX_CONTEXT_OUT" || adk_nix_die "cannot create $ADK_NIX_CONTEXT_OUT"
        cd "$ADK_NIX_CONTEXT_OUT" || adk_nix_die "cannot enter $ADK_NIX_CONTEXT_OUT"
        "$venv/bin/python" "$ADK_NIX_LIB/capture_context.py" "$ADK_NIX_CONTEXT_OUT/nix-context.json" \
            || adk_nix_die "context capture failed; nothing was run"
    fi
    adk_nix_note "$ADK_NIX_MODE shell ready: venv $venv, OpenROAD $OPENROAD, KLayout $ADK_NIX_KLAYOUT"
}

# Where this shell's venv lives: one path per interpreter, Nix environment and
# set of trees. dev reuses what is there, and reinstalls only when the stamp
# below changes; its trees are keyed by path. evidence keys each tree by name,
# install directory and commit, not by where the checkout is: two clones of one
# commit under one ADK_NIX_STATE get one venv path, so the console scripts the
# tool table keys are the same bytes. The path stays the same across entries
# too, because the flow records the interpreter's path in a run's identity and
# resume refuses a changed one. adk_nix_venv_claim makes it new.
adk_nix_venv_dir() {
    local trees="$1" key venv lines="$1" name tree sub head
    [ -n "${ADK_NIX_STATE:-}" ] || adk_nix_die "ADK_NIX_STATE is not set"
    mkdir -p "$ADK_NIX_STATE" || adk_nix_die "cannot create $ADK_NIX_STATE"
    if [ "$ADK_NIX_MODE" = evidence ]; then
        lines=""
        while IFS='|' read -r name tree sub; do
            [ -n "$name" ] || continue
            head="$(adk_nix_git -C "$tree" rev-parse HEAD 2>/dev/null)" || \
                adk_nix_die "$name: cannot read HEAD in $tree"
            lines+="$name|$sub|$head"$'\n'
        done <<< "$trees"
    fi
    key="$(printf '%s\n' "$ADK_NIX_PYTHON" "$ADK_NIX_SITE_PACKAGES" "$lines" | sha256sum | cut -c1-16)"
    [[ "$key" =~ ^[0-9a-f]{16}$ ]] || adk_nix_die "cannot key the venv"
    if [ "$ADK_NIX_MODE" = evidence ]; then
        venv="$ADK_NIX_STATE/evidence-venv-$key"
    else
        venv="$ADK_NIX_STATE/venv-$key"
    fi
    echo "$venv"
}

# Evidence mode: take the venv's lock and make the venv new, so the install and
# pip check always run and nothing an earlier shell left can be imported. The
# lock is held for this shell's life: an exclusive flock(2) on a descriptor this
# shell keeps open (taken through the interpreter; the shell has no flock(1)),
# which every child inherits and which is released when the last holder exits.
# A second evidence shell on the same venv refuses instead of emptying a venv
# that is in use. Called in this shell, not a subshell, so the descriptor stays.
adk_nix_venv_claim() {
    local venv="$1"
    exec {ADK_NIX_VENV_LOCK}>>"$venv.lock" || adk_nix_die "cannot open $venv.lock"
    "$ADK_NIX_PYTHON" -I -c 'import fcntl, sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)' \
        "$ADK_NIX_VENV_LOCK" 2>/dev/null || \
        adk_nix_die "evidence: $venv is held by another evidence shell ($venv.lock)"
    rm -rf -- "$venv" || adk_nix_die "cannot remove the previous $venv"
    mkdir -- "$venv" || adk_nix_die "cannot create $venv"
}

# The two store paths this shell names from staging.pin (the flake's copy):
# the lane's interpreter env, which the site-packages must sit under, and the
# co-simulation devshell, which COSIM_DEVSHELL is. Each must also be in this
# shell's closure.
adk_nix_check_pinned_paths() {
    local python_pin devshell_pin
    [ -f "$ADK_NIX_STAGING_PIN" ] || adk_nix_die "no staging.pin at $ADK_NIX_STAGING_PIN"
    python_pin="$(adk_nix_pin "$ADK_NIX_STAGING_PIN" NIX_PYTHON_OUTPATH)" || \
        adk_nix_die "$ADK_NIX_STAGING_PIN does not source cleanly"
    devshell_pin="$(adk_nix_pin "$ADK_NIX_STAGING_PIN" NIX_COSIM_DEVSHELL_OUTPATH)" || \
        adk_nix_die "$ADK_NIX_STAGING_PIN does not source cleanly"
    [ -n "$python_pin" ] || adk_nix_die "python: staging.pin names no NIX_PYTHON_OUTPATH"
    case "$ADK_NIX_SITE_PACKAGES" in
        "$python_pin"/?*) ;;
        *) adk_nix_die "python: ADK_NIX_SITE_PACKAGES=$ADK_NIX_SITE_PACKAGES is not under NIX_PYTHON_OUTPATH $python_pin" ;;
    esac
    adk_nix_in_closure python "$ADK_NIX_SITE_PACKAGES" >/dev/null || exit 1
    [ -n "$devshell_pin" ] && [ "$ADK_NIX_COSIM_DEVSHELL" = "$devshell_pin" ] || \
        adk_nix_die "cosim: ADK_NIX_COSIM_DEVSHELL=$ADK_NIX_COSIM_DEVSHELL is not NIX_COSIM_DEVSHELL_OUTPATH ${devshell_pin:-(none)}"
    adk_nix_in_closure cosim "$ADK_NIX_COSIM_DEVSHELL" >/dev/null || exit 1
}

# The tool identity table packaging-flow keys a run's tools by, written by
# tool_identity.py lane from this shell's closureInfo (no store query), and the
# pair packaging-flow reads: ADK_TOOLS_TABLE, the table's absolute path, and
# ADK_TOOLS_CLOSURE, sha256:<its closure digest>. Its rows: each staging.pin
# store path in the closure; git, the one on PATH by its realpath, which must be
# in the closure too; and in evidence mode chiplet-cosim, by its console script
# and its staged checkout's commit and tracked digest. A dev tree can hold any
# edit, so dev leaves that row out: a kind that keys it reads UNRESOLVED, and
# the entry goes on.
adk_nix_tool_table() {
    local venv="$1" trees="$2" git_bin git_version cosim table closure args=()
    git_bin="$(command -v git)" || adk_nix_die "git: not on PATH"
    git_bin="$(adk_nix_in_closure git "$git_bin")" || exit 1
    git_version="$("$git_bin" --version)" || adk_nix_die "git: $git_bin --version failed"
    args+=(--tool "git=$git_version:$git_bin")
    if [ "$ADK_NIX_MODE" = evidence ]; then
        cosim="$(awk -F'|' '$1 == "chiplet-cosim" { print $2 }' <<< "$trees")"
        # tool_identity.py splits the spec at ':'.
        case "$venv$cosim" in
            *:*) adk_nix_die "chiplet-cosim: $venv or $cosim holds a ':', which the table's spec cannot carry" ;;
        esac
        args+=(--tracked-tool "chiplet-cosim=$venv/bin/chiplet-cosim:$cosim")
    fi
    table="$ADK_NIX_TMP/tool-identity.json"
    "$ADK_NIX_PYTHON" -I -B "$ADK_NIX_LIB/tool_identity.py" lane \
        --closure-info "$ADK_NIX_CLOSURE_INFO" --pin "$ADK_NIX_STAGING_PIN" "${args[@]}" \
        --out "$table" >&2 || adk_nix_die "the tool identity table was not written"
    closure="$("$ADK_NIX_PYTHON" -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["closure_sha256"])' \
        "$table")" || adk_nix_die "cannot read the closure digest of $table"
    [[ "$closure" =~ ^[0-9a-f]{64}$ ]] || adk_nix_die "$table's closure digest is not a sha256: $closure"
    export ADK_TOOLS_TABLE="$table" ADK_TOOLS_CLOSURE="sha256:$closure"
}

# The stamp that decides whether the editable install is current: the
# interpreter, each tree's path, and the files that define its metadata. Not
# the commit: no package here takes its version from git, and the flow reads
# the commit itself at run time. A Python-only edit, committed or not, changes
# none of these, so it costs no install.
adk_nix_stamp_from() {
    local trees="$1" name tree sub f
    {
        echo "$ADK_NIX_PYTHON"
        echo "$ADK_NIX_SITE_PACKAGES"
        while IFS='|' read -r name tree sub; do
            [ -n "$name" ] || continue
            echo "$name $tree"
            # chiplet-format-io takes its version from the module itself.
            for f in pyproject.toml setup.py setup.cfg chiplet_format_io/__init__.py; do
                if [ -f "$tree/$sub/$f" ]; then
                    sha256sum "$tree/$sub/$f"
                fi
            done
        done <<< "$trees"
    } | sha256sum | cut -d' ' -f1
}

if [ -z "${ADK_NIX_LIB_ONLY:-}" ]; then
    adk_nix_main
fi
