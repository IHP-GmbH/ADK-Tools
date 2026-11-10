# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Sourced by the shellHook of devShells.tail (flake.nix), after nix/cli-env.sh
# has made the CLI shell; not run on its own. It adds what chiplet-flow's tail
# (gds, interposer-drc, drc) needs on top of that shell and nothing else:
#
#   chiplet-export, adk-verify-carrier-drc, adk-drc
#                   the image's three commands, by the image's names, from
#                   packages.tail-wrappers (nix-tail/*.in); the flow finds them
#                   on PATH. The hook refuses if PATH resolves any of them to
#                   another file.
#   kicad-cli       the KiCad fork's, found on PATH by the schematic check
#                   (gds_to_kicad's chiplet_to_kicad_sch.py --check), as the
#                   image's /usr/bin/kicad-cli is. The fork's bin directory is
#                   appended to PATH whole: every name in it is KiCad's own, so
#                   it shadows nothing already found. The hook refuses if PATH
#                   resolves kicad-cli to another file.
#   ADK_NIX_PLUGIN_ROOT
#                   the chiplet_kicad_plugin tree chiplet-export runs. Evidence
#                   mode: tools/chiplet_kicad_plugin of the adk-tools checkout,
#                   clean at its gitlink. Dev mode: the caller's, used as it
#                   is. The image's name for it, CHIPLET_KICAD_PLUGIN_ROOT, is
#                   among the variables cli-env.sh clears before this runs, and
#                   is left cleared: the flow reads it only to find the board
#                   observer, which needs pcbnew in the flow's own interpreter.
#   PDK_ROOT        packages.sg13g2-pdk. The export's worker places the SG13G2
#                   via_stack PCells from it, and draws plain rectangles when it
#                   cannot. The image sets no PDK, so neither does this.
#   KICAD_CHIPLET_PYTHON
#                   the worker chiplet-export starts for hyp_to_gds.py: the CLI
#                   shell's venv interpreter (klayout.db and PyYAML from Nix).
#   GDS_TO_KICAD_ROOT
#                   the gds_to_kicad tree the worker imports PinList from, for
#                   every die with pin lists (the interposer PDK's
#                   bump_mirror.py). Evidence mode: tools/gds_to_kicad of the
#                   adk-tools checkout, clean at its gitlink. Dev mode: the
#                   caller's ADK_NIX_GDS_TO_KICAD_ROOT, used as it is. Always
#                   set, so that bump_mirror never falls back to walking up
#                   from its own file for a gds_to_kicad directory, which finds
#                   whatever checkout sits above.
#
# cli-env.sh clears PDK_ROOT, KICAD_CHIPLET_PYTHON and GDS_TO_KICAD_ROOT with
# the host's other variables; they are set again here from what the flake or
# the caller declared. The hook also clears host variables read on the export's
# path that the cli hook leaves (ADK_NIX_TAIL_SCRUB, and any variable named
# KLAYOUT in any case, which switches on the PCell libraries' KLAYOUT define).
# NIX_PYTHONPATH is one of them and not only the export's: every interpreter of
# the cli venv reads it. A development entry of the cli shell passes the host's
# value on, and in a development entry of this shell the cli hook sets up the
# venv (pip included) before this hook clears it.
#
# In evidence mode it then runs nix-tail/capture_tail_context.py, which asks
# the export interpreter and the worker what they load, records the kicad-cli
# PATH resolves and writes tail-context.json beside the CLI shell's
# nix-context.json; a problem there refuses the shell.
#
# These files are not under nix/ because the cli and build shells carry that
# directory whole (ADK_NIX_LIB): a file added there would change both.
#
# Inputs from flake.nix: ADK_NIX_TAIL_WRAPPERS, ADK_NIX_TAIL_EXPORT_PYTHON,
# ADK_NIX_KICAD_FORK_BASE, ADK_NIX_SG13G2_PDK, ADK_NIX_TAIL_LIB. From cli-env.sh:
# ADK_NIX_MODE, ADK_NIX_VENV, ADK_ROOT, and in evidence mode the caller's
# ADK_NIX_ADK_TOOLS and ADK_NIX_CONTEXT_OUT. From the caller in dev mode:
# ADK_NIX_PLUGIN_ROOT and ADK_NIX_GDS_TO_KICAD_ROOT. ADK_NIX_STORE_DIR (default
# /nix/store) exists for nix-tail/test-tail-env.sh; evidence mode refuses any
# other value.

# The store-path and plain-path checks the build hook uses, without its main.
# Set and put back by hand: an assignment written before `.` stays set
# afterwards in a POSIX-mode bash, and would then skip this hook's main.
adk_nix_tail_lib_only="${ADK_NIX_LIB_ONLY:-}"
ADK_NIX_LIB_ONLY=1
# shellcheck disable=SC1091
. "$ADK_NIX_LIB/build-env.sh"
if [ -n "$adk_nix_tail_lib_only" ]; then
    ADK_NIX_LIB_ONLY="$adk_nix_tail_lib_only"
else
    unset ADK_NIX_LIB_ONLY
fi

# The image's tail commands, in the order chiplet-flow requires them.
ADK_NIX_TAIL_COMMANDS="chiplet-export adk-verify-carrier-drc adk-drc"
ADK_NIX_TAIL_PLUGIN_PATH=tools/chiplet_kicad_plugin
ADK_NIX_TAIL_CLI=plugins/chiplet_export/chiplet_export_cli.py
ADK_NIX_TAIL_PYCELL_LIB=ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib
ADK_NIX_TAIL_G2K_PATH=tools/gds_to_kicad
# The file bump_mirror.py requires in the root it is given.
ADK_NIX_TAIL_G2K_FILE=pin_list.py
# Read on the export's path and not by the cli shell's scrub: the SG13G2 PCell
# library takes its layer table from KLAYOUT_LYP_FILE, the store Python's
# sitecustomize.py adds site directories from NIX_PYTHONPATH, and both PCell
# libraries print from the IHP_PYCELL_LIB_PRINT_ pair.
ADK_NIX_TAIL_SCRUB="KLAYOUT_LYP_FILE NIX_PYTHONPATH
IHP_PYCELL_LIB_PRINT_PROCESS_TREE IHP_PYCELL_LIB_PRINT_DEFINES_SET"

# The plugin tree, resolved and checked, on stdout.
adk_nix_tail_plugin() {
    local plugin want
    if [ "$ADK_NIX_MODE" = evidence ]; then
        plugin="$ADK_NIX_ADK_TOOLS/$ADK_NIX_TAIL_PLUGIN_PATH"
        # Before the pin, for the reason given for gds_to_kicad below.
        [ -f "$plugin/$ADK_NIX_TAIL_CLI" ] || \
            adk_nix_die "chiplet_kicad_plugin: no $ADK_NIX_TAIL_CLI under $plugin; initialise the $ADK_NIX_TAIL_PLUGIN_PATH submodule"
        # From HEAD, which is the flake's revision, as for the roots.
        want="$(adk_nix_gitlink "$ADK_NIX_ADK_TOOLS" "$ADK_NIX_TAIL_PLUGIN_PATH")"
        adk_nix_require_pinned "chiplet_kicad_plugin ($ADK_NIX_TAIL_PLUGIN_PATH)" \
            "$plugin" "$want" root
    else
        plugin="${ADK_NIX_PLUGIN_ROOT:-}"
        [ -n "$plugin" ] || \
            adk_nix_die "dev: set ADK_NIX_PLUGIN_ROOT to a chiplet_kicad_plugin checkout (no fallback is guessed)"
        [ -f "$plugin/$ADK_NIX_TAIL_CLI" ] || adk_nix_die "no $ADK_NIX_TAIL_CLI under $plugin"
    fi
    realpath -- "$plugin" || adk_nix_die "cannot resolve $plugin"
}

# The gds_to_kicad tree, resolved and checked, on stdout.
adk_nix_tail_gds_to_kicad() {
    local tree want
    if [ "$ADK_NIX_MODE" = evidence ]; then
        tree="$ADK_NIX_ADK_TOOLS/$ADK_NIX_TAIL_G2K_PATH"
        # Before the pin: a submodule never initialised is an empty directory
        # that git answers for as the adk-tools checkout, so the pin check
        # would name a commit instead of the missing tree.
        [ -f "$tree/$ADK_NIX_TAIL_G2K_FILE" ] || \
            adk_nix_die "gds_to_kicad: no $ADK_NIX_TAIL_G2K_FILE under $tree; initialise the $ADK_NIX_TAIL_G2K_PATH submodule"
        want="$(adk_nix_gitlink "$ADK_NIX_ADK_TOOLS" "$ADK_NIX_TAIL_G2K_PATH")"
        adk_nix_require_pinned "gds_to_kicad ($ADK_NIX_TAIL_G2K_PATH)" "$tree" "$want" root
    else
        tree="${ADK_NIX_GDS_TO_KICAD_ROOT:-}"
        [ -n "$tree" ] || \
            adk_nix_die "dev: set ADK_NIX_GDS_TO_KICAD_ROOT to a gds_to_kicad checkout (no fallback is guessed)"
        [ -f "$tree/$ADK_NIX_TAIL_G2K_FILE" ] || \
            adk_nix_die "no $ADK_NIX_TAIL_G2K_FILE under $tree"
    fi
    realpath -- "$tree" || adk_nix_die "cannot resolve $tree"
}

# Clear ADK_NIX_TAIL_SCRUB and every variable named KLAYOUT in any
# case: the PCell libraries set a define for each of their '#ifdef' names that
# an environment variable matches, case-insensitively.
adk_nix_tail_scrub() {
    # The list is split on blanks and newlines whatever the caller's IFS.
    local v IFS=$' \t\n'
    for v in $ADK_NIX_TAIL_SCRUB; do
        unset "$v"
    done
    # Every variable whose name starts with k or K, word by word whatever IFS
    # is, without compgen.
    for v in "${!k@}" "${!K@}"; do
        [ "${v,,}" != klayout ] || unset "$v"
    done
}

adk_nix_tail_main() {
    local var cmd found plugin g2k
    for var in ADK_NIX_TAIL_WRAPPERS ADK_NIX_TAIL_EXPORT_PYTHON ADK_NIX_KICAD_FORK_BASE \
               ADK_NIX_SG13G2_PDK ADK_NIX_TAIL_LIB ADK_NIX_MODE ADK_NIX_VENV ADK_ROOT; do
        [ -n "${!var:-}" ] || adk_nix_die "$var is not set; enter through devShells.tail"
    done
    if [ "$ADK_NIX_MODE" = evidence ]; then
        [ "$ADK_NIX_STORE_DIR" = /nix/store ] || adk_nix_die "evidence: ADK_NIX_STORE_DIR must be /nix/store"
        for var in ADK_NIX_ADK_TOOLS ADK_NIX_CONTEXT_OUT; do
            [ -n "${!var:-}" ] || adk_nix_die "evidence: $var is not set"
        done
    fi
    for var in ADK_NIX_TAIL_WRAPPERS ADK_NIX_TAIL_EXPORT_PYTHON ADK_NIX_KICAD_FORK_BASE \
               ADK_NIX_SG13G2_PDK; do
        adk_nix_store_path "${!var}" || adk_nix_die "$var=${!var} is not a store path"
    done
    [ -x "$ADK_NIX_TAIL_EXPORT_PYTHON/bin/python" ] || \
        adk_nix_die "no $ADK_NIX_TAIL_EXPORT_PYTHON/bin/python"
    [ -d "$ADK_NIX_SG13G2_PDK/$ADK_NIX_TAIL_PYCELL_LIB" ] || \
        adk_nix_die "no $ADK_NIX_TAIL_PYCELL_LIB under $ADK_NIX_SG13G2_PDK"
    [ -x "$ADK_NIX_VENV/bin/python" ] || adk_nix_die "no $ADK_NIX_VENV/bin/python"

    plugin="$(adk_nix_tail_plugin)" || exit 1
    g2k="$(adk_nix_tail_gds_to_kicad)" || exit 1
    adk_nix_tail_scrub
    export ADK_NIX_PLUGIN_ROOT="$plugin"
    export GDS_TO_KICAD_ROOT="$g2k"
    export PDK_ROOT="$ADK_NIX_SG13G2_PDK"
    export KICAD_CHIPLET_PYTHON="$ADK_NIX_VENV/bin/python"

    # Resolved as chiplet-flow resolves them, from this shell's PATH: the CLI
    # shell's venv is first on it, and a host command of the same name must
    # not be the one found.
    for cmd in $ADK_NIX_TAIL_COMMANDS; do
        found="$(type -P "$cmd")" || adk_nix_die "$cmd is not on PATH"
        [ "$found" = "$ADK_NIX_TAIL_WRAPPERS/bin/$cmd" ] || \
            adk_nix_die "$cmd resolves to $found, not $ADK_NIX_TAIL_WRAPPERS/bin/$cmd"
    done

    # Last on PATH, and still the one found: a kicad-cli ahead of it is
    # refused as a host command of the tail's names is.
    [ -x "$ADK_NIX_KICAD_FORK_BASE/bin/kicad-cli" ] || \
        adk_nix_die "no $ADK_NIX_KICAD_FORK_BASE/bin/kicad-cli"
    export PATH="$PATH:$ADK_NIX_KICAD_FORK_BASE/bin"
    found="$(type -P kicad-cli)"
    [ "$found" = "$ADK_NIX_KICAD_FORK_BASE/bin/kicad-cli" ] || \
        adk_nix_die "kicad-cli resolves to $found, not $ADK_NIX_KICAD_FORK_BASE/bin/kicad-cli"

    if [ "$ADK_NIX_MODE" = evidence ]; then
        # The CLI hook left the shell in the context directory, beside
        # nix-context.json. -I -S: this interpreter reads no PYTHON* variable
        # and no site directory; the probes it starts get this shell's
        # environment unchanged.
        "$ADK_NIX_VENV/bin/python" -I -S "$ADK_NIX_TAIL_LIB/capture_tail_context.py" \
            "$ADK_NIX_CONTEXT_OUT/tail-context.json" \
            || adk_nix_die "tail context capture failed; nothing was run"
    fi
    adk_nix_note "tail shell ready: chiplet-export on $ADK_NIX_TAIL_EXPORT_PYTHON, plugin $ADK_NIX_PLUGIN_ROOT, PDK_ROOT $PDK_ROOT, gds_to_kicad $GDS_TO_KICAD_ROOT, kicad-cli $found"
}

if [ -z "$adk_nix_tail_lib_only" ]; then
    adk_nix_tail_main
fi
