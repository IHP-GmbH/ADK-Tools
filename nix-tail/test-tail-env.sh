#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for nix-tail/tail-env.sh and the wrappers, without Nix. The hook is
# sourced after nix/cli-env.sh's functions, as the tail shell's hook runs after
# the CLI shell's, against a stand-in store under one mktemp directory: the
# three wrappers made from nix-tail/*.in as the flake's `substitute` makes
# them, an export interpreter and a venv interpreter that record what they
# were started with, a fork, a PDK and a plugin tree. The wrappers are run for
# real, so what reaches the interpreters is observed, not read from their
# text. Evidence mode's plugin and gds_to_kicad checks run against stand-in
# adk-tools checkouts with a gitlink at tools/chiplet_kicad_plugin and at
# tools/gds_to_kicad.
#
# Needs bash, git, python3 and coreutils. Writes only under one mktemp
# directory.
#
# Usage: nix-tail/test-tail-env.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
lib="$(cd "$here/../nix" && pwd)"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-tail-env-test.XXXXXX")"
trap 'chmod -R u+w "$work" 2>/dev/null; rm -rf "$work"' EXIT
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

# expect_eq <description> <actual> <expected>
expect_eq() {
    if [ "$2" = "$3" ]; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $1"
        echo "    got:      $2"
        echo "    expected: $3"
    fi
}

h32() { printf '%032d' "$1"; }
g() { git -C "$@"; }

store="$work/store"
mkdir -p "$store"

# An interpreter that records its own path, its arguments one per line and
# ADK_PYTHON, into $work/<name>.log, and exits 0. A sh script, which reads no
# BASH_ENV, so that the wrapper is the only bash that could.
recorder() {
    local path="$1" name="$2"
    mkdir -p "$(dirname "$path")"
    cat > "$path" <<EOF
#!/bin/sh
{ echo "exe=\$0"; for a in "\$@"; do echo "arg=\$a"; done; echo "ADK_PYTHON=\${ADK_PYTHON-unset}"; } > "$work/$name.log"
EOF
    chmod +x "$path"
}

export_python="$store/$(h32 1)-adk-tail-export-python"
fork="$store/$(h32 2)-kicad-base-5d12a25667"
pdk="$store/$(h32 3)-ihp-open-pdk-22f4335"
wrappers="$store/$(h32 4)-adk-tail-wrappers"
carrier="$store/$(h32 5)-adk-verify-carrier-drc"
recorder "$export_python/bin/python" export
mkdir -p "$fork/lib" "$fork/bin" "$pdk/ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib"
printf '#!/bin/sh\nexit 0\n' > "$fork/bin/kicad-cli"
chmod +x "$fork/bin/kicad-cli"
printf '#!/usr/bin/env python3\n' > "$carrier"
venv="$work/venv"
recorder "$venv/bin/python" venv
adk="$work/adk"
mkdir -p "$adk/klayout/drc"
plugin="$work/plugin"
mkdir -p "$plugin/plugins/chiplet_export"
touch "$plugin/plugins/chiplet_export/chiplet_export_cli.py"
g2k="$work/g2k"
mkdir -p "$g2k"
touch "$g2k/pin_list.py"

# The wrappers as the flake makes them: each template with its placeholders
# filled, mode 0555.
mkdir -p "$wrappers/bin"
for name in chiplet-export adk-verify-carrier-drc adk-drc; do
    python3 - "$here/$name.in" "$wrappers/bin/$name" "$(command -v bash)" "$export_python" "$carrier" <<'EOF'
import sys
src, dst, bash, export, carrier = sys.argv[1:6]
text = open(src).read()
for key, value in (("bash", bash), ("exportPython", export), ("carrierDrc", carrier)):
    text = text.replace(f"@{key}@", value)
open(dst, "w").write(text)
EOF
    chmod 0555 "$wrappers/bin/$name"
done
chmod 0555 "$wrappers/bin"

base_path="$wrappers/bin:$PATH"

# in_tail <script>: bash with the stand-in tail shell's inputs, the CLI hook's
# functions, then the tail hook, then <script>. Each ADK_ variable can be
# overridden or unset (VAR=) through the environment of the call; T_HOST_G2K is
# a GDS_TO_KICAD_ROOT that reaches the hook, which the cli hook clears first.
in_tail() {
    env ADK_NIX_STORE_DIR="$store" ADK_NIX_LIB="$lib" ADK_NIX_TAIL_LIB="$here" \
        ADK_NIX_TAIL_WRAPPERS="${T_WRAPPERS-$wrappers}" \
        ADK_NIX_TAIL_EXPORT_PYTHON="${T_EXPORT-$export_python}" \
        ADK_NIX_KICAD_FORK_BASE="${T_FORK-$fork}" ADK_NIX_SG13G2_PDK="${T_PDK-$pdk}" \
        ADK_NIX_MODE="${T_MODE-dev}" ADK_NIX_VENV="${T_VENV-$venv}" ADK_ROOT="${T_ADK-$adk}" \
        ADK_NIX_PLUGIN_ROOT="${T_PLUGIN-$plugin}" ADK_NIX_GDS_TO_KICAD_ROOT="${T_G2K-$g2k}" \
        PATH="${T_PATH-$base_path}" PDK_ROOT= KICAD_CHIPLET_PYTHON= GDS_TO_KICAD_ROOT="${T_HOST_G2K-}" \
        bash -c '
set -u
ADK_NIX_LIB_ONLY=1 . "$ADK_NIX_LIB/cli-env.sh"
. "$ADK_NIX_TAIL_LIB/tail-env.sh"
'"$1"
}

field() { sed -n "s/^$2=//p" "$work/$1.log"; }
args() { sed -n 's/^arg=//p' "$work/$1.log"; }

# --- the shell -------------------------------------------------------------

expect 0 "dev mode with every input passes" in_tail 'echo "PDK_ROOT=$PDK_ROOT"; echo "WORKER=$KICAD_CHIPLET_PYTHON"; echo "PLUGIN=$ADK_NIX_PLUGIN_ROOT"; echo "LIB_ONLY=${ADK_NIX_LIB_ONLY-unset}"'
expect_out "PDK_ROOT is the store PDK" "^PDK_ROOT=$pdk\$"
expect_out "the worker is the venv interpreter" "^WORKER=$venv/bin/python\$"
expect_out "the plugin root is resolved" "^PLUGIN=$plugin\$"
expect_out "the hook leaves no lib-only flag behind" "^LIB_ONLY=unset\$"
expect_out "the hook says what it set" "tail shell ready: chiplet-export on $export_python, plugin $plugin, PDK_ROOT $pdk, gds_to_kicad $g2k, kicad-cli $fork/bin/kicad-cli\$"

expect 0 "kicad-cli is the fork's" in_tail 'echo "KICAD_CLI=$(type -P kicad-cli)"; echo "LAST=${PATH##*:}"'
expect_out "found through PATH" "^KICAD_CLI=$fork/bin/kicad-cli\$"
expect_out "the fork's bin is last on PATH" "^LAST=$fork/bin\$"

# The host's own root, and every variable the hook clears, set on the way in.
# KLAYOUT_PATH and KLAYOUT_HOME stand for the cli hook's own, which must stay.
expect 0 "the gds_to_kicad root and the cleared variables" env T_HOST_G2K=/host/gds_to_kicad \
    KLAYOUT_LYP_FILE=/host/sg13g2.lyp NIX_PYTHONPATH=/host/site IHP_PYCELL_LIB_PRINT_PROCESS_TREE=1 \
    IHP_PYCELL_LIB_PRINT_DEFINES_SET=1 klayout=1 KLayout=1 KLAYOUTX=1 \
    KLAYOUT_PATH=/cli/klayout-path KLAYOUT_HOME=/cli/klayout-home \
    bash -c "$(declare -f in_tail); $(declare -p lib here store wrappers export_python fork pdk venv adk plugin g2k base_path); in_tail 'echo \"G2K=\$GDS_TO_KICAD_ROOT\"; echo \"KEPT=\$KLAYOUT_PATH \$KLAYOUT_HOME\"; env | LC_ALL=C sort | sed -n \"s/^\\(KLAYOUT_LYP_FILE\\|NIX_PYTHONPATH\\|IHP_PYCELL_LIB_[A-Z_]*\\|[Kk][Ll][Aa][Yy][Oo][Uu][Tt]X\\?\\)=.*/LEFT=\\1/p\"'"
expect_out "GDS_TO_KICAD_ROOT is the caller's tree, not the host's" "^G2K=$g2k\$"
expect_eq "only a name that is not KLAYOUT is left" "$(sed -n 's/^LEFT=//p' "$work/last.out")" "KLAYOUTX"
expect_out "KLAYOUT_PATH and KLAYOUT_HOME are kept" "^KEPT=/cli/klayout-path /cli/klayout-home\$"

# Neither the caller's IFS nor a missing compgen changes what is cleared.
expect 0 "the clearing with another IFS and without compgen" env ADK_NIX_LIB="$lib" \
    KLAYOUT_LYP_FILE=/host/sg13g2.lyp NIX_PYTHONPATH=/host/site KLayout=1 KLAYOUT_HOME=/cli/klayout-home bash -c '
ADK_NIX_LIB_ONLY=1 . "$ADK_NIX_LIB/cli-env.sh"
ADK_NIX_LIB_ONLY=1
. '"$here"'/tail-env.sh
IFS=:
enable -n compgen
adk_nix_tail_scrub || exit 1
env | LC_ALL=C sort | sed -n "s/^\(KLAYOUT_LYP_FILE\|NIX_PYTHONPATH\|KLayout\|KLAYOUT_HOME\)=.*/LEFT=\1/p"'
expect_eq "only KLAYOUT_HOME is left" "$(sed -n 's/^LEFT=//p' "$work/last.out")" "KLAYOUT_HOME"

ln -s "$g2k" "$work/g2k-link"
expect 0 "a gds_to_kicad root given through a link" env T_G2K="$work/g2k-link" bash -c "$(declare -f in_tail); $(declare -p lib here store wrappers export_python fork pdk venv adk plugin g2k base_path); in_tail 'echo \"G2K=\$GDS_TO_KICAD_ROOT\"'"
expect_out "is exported as its real path" "^G2K=$g2k\$"

ln -s "$plugin" "$work/plugin-link"
expect 0 "a plugin root given through a link" env T_PLUGIN="$work/plugin-link" bash -c "$(declare -f in_tail); $(declare -p lib here store wrappers export_python fork pdk venv adk plugin g2k base_path); in_tail 'echo \"PLUGIN=\$ADK_NIX_PLUGIN_ROOT\"'"
expect_out "is exported as its real path" "^PLUGIN=$plugin\$"

expect 0 "sourced lib-only, the hook sets nothing" env -u PDK_ROOT ADK_NIX_LIB="$lib" bash -c '
ADK_NIX_LIB_ONLY=1 . "$ADK_NIX_LIB/cli-env.sh"
ADK_NIX_LIB_ONLY=1
. '"$here"'/tail-env.sh
[ "$ADK_NIX_LIB_ONLY" = 1 ] && [ -z "${PDK_ROOT:-}" ] && type adk_nix_tail_main >/dev/null'

# --- the wrappers, run for real --------------------------------------------

rm -f "$work"/*.log
expect 0 "chiplet-export runs" in_tail 'chiplet-export --board "a b.kicad_pcb" --output-dir "" "--x=1"'
expect_eq "under the export interpreter" "$(field export exe)" "$export_python/bin/python"
expect_eq "with the plugin's CLI first and the arguments whole" "$(args export)" \
    "$(printf '%s\n' "$plugin/plugins/chiplet_export/chiplet_export_cli.py" --board "a b.kicad_pcb" --output-dir "" "--x=1")"

expect 0 "adk-drc runs" in_tail 'adk-drc --path "x y.gds" --report r.json'
expect_eq "under the venv interpreter" "$(field venv exe)" "$venv/bin/python"
expect_eq "with the ADK's run_drc.py first and the arguments whole" "$(args venv)" \
    "$(printf '%s\n' "$adk/klayout/drc/run_drc.py" --path "x y.gds" --report r.json)"

rm -f "$work/venv.log"
expect 0 "adk-verify-carrier-drc runs" in_tail 'adk-verify-carrier-drc --gds g.gds --pdk-root "p q"'
expect_eq "with the store copy of the script first and the arguments whole" "$(args venv)" \
    "$(printf '%s\n' "$carrier" --gds g.gds --pdk-root "p q")"
expect_eq "and ADK_PYTHON set to the venv interpreter" "$(field venv ADK_PYTHON)" "$venv/bin/python"

expect 1 "chiplet-export outside the shell refuses" env -u ADK_NIX_PLUGIN_ROOT "$wrappers/bin/chiplet-export" --board b
expect_out "and says where to enter" "ADK_NIX_PLUGIN_ROOT: enter through devShells.tail"
expect 1 "chiplet-export without PDK_ROOT refuses" env -u PDK_ROOT ADK_NIX_PLUGIN_ROOT="$plugin" \
    KICAD_CHIPLET_PYTHON="$venv/bin/python" "$wrappers/bin/chiplet-export" --board b
expect_out "and names it" "PDK_ROOT: enter through devShells.tail"
expect 1 "chiplet-export without KICAD_CHIPLET_PYTHON refuses" env -u KICAD_CHIPLET_PYTHON \
    ADK_NIX_PLUGIN_ROOT="$plugin" PDK_ROOT="$pdk" "$wrappers/bin/chiplet-export" --board b
expect_out "and names it" "KICAD_CHIPLET_PYTHON: enter through devShells.tail"
rm -f "$work/export.log"
expect 1 "chiplet-export without GDS_TO_KICAD_ROOT refuses" env -u GDS_TO_KICAD_ROOT \
    ADK_NIX_PLUGIN_ROOT="$plugin" PDK_ROOT="$pdk" KICAD_CHIPLET_PYTHON="$venv/bin/python" \
    "$wrappers/bin/chiplet-export" --board b
expect_out "and names it" "GDS_TO_KICAD_ROOT: enter through devShells.tail"
expect 1 "and the export interpreter never started" test -e "$work/export.log"
expect 1 "adk-drc outside the shell refuses" env -u ADK_NIX_VENV -u ADK_ROOT "$wrappers/bin/adk-drc" --path p
expect 1 "adk-verify-carrier-drc outside the shell refuses" env -u ADK_NIX_VENV "$wrappers/bin/adk-verify-carrier-drc"

printf 'echo BASH_ENV was read\n' > "$work/bash_env"
rm -f "$work/venv.log"
expect 0 "a wrapper reads no BASH_ENV" env BASH_ENV="$work/bash_env" ADK_NIX_VENV="$venv" ADK_ROOT="$adk" "$wrappers/bin/adk-drc" --path p
if grep -q "BASH_ENV was read" "$work/last.out"; then
    failed=$((failed + 1)); echo "FAIL: BASH_ENV was read by a wrapper"
else
    passed=$((passed + 1))
fi
# The same wrapper without -p reads it: the check above can fail.
sed '1s/ -p$//' "$wrappers/bin/adk-drc" > "$work/adk-drc-no-p"
chmod +x "$work/adk-drc-no-p"
expect 0 "without -p the same wrapper runs" env BASH_ENV="$work/bash_env" ADK_NIX_VENV="$venv" ADK_ROOT="$adk" "$work/adk-drc-no-p" --path p
expect_out "and reads BASH_ENV" "BASH_ENV was read"

# --- refusals, one break each ----------------------------------------------

for var in T_WRAPPERS T_EXPORT T_FORK T_PDK T_VENV T_ADK; do
    expect 1 "an empty $var refuses" env "$var=" bash -c "$(declare -f in_tail); $(declare -p lib here store wrappers export_python fork pdk venv adk plugin g2k base_path); in_tail true"
    expect_out "the refusal says to enter through the shell" "is not set; enter through devShells.tail"
done

outside="$work/elsewhere/$(h32 9)-ihp-open-pdk"
mkdir -p "$outside/ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib"
T_PDK="$outside" expect 1 "a PDK outside the store refuses" in_tail true
expect_out "the refusal names it" "ADK_NIX_SG13G2_PDK=$outside is not a store path"

T_EXPORT="$store/$(h32 6)-adk-tail-export-python" expect 1 "an export interpreter that does not exist refuses" in_tail true
expect_out "the refusal names it" "no $store/$(h32 6)-adk-tail-export-python/bin/python"

T_PDK="$store/$(h32 7)-ihp-open-pdk" expect 1 "a PDK without the PCell library refuses" in_tail true
expect_out "the refusal names the library" "no ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib under"

T_VENV="$work/no-venv" expect 1 "a venv without its interpreter refuses" in_tail true

T_PLUGIN="" expect 1 "dev mode without a plugin root refuses" in_tail true
expect_out "the refusal says what to set" "dev: set ADK_NIX_PLUGIN_ROOT to a chiplet_kicad_plugin checkout"

T_G2K="" expect 1 "dev mode without a gds_to_kicad root refuses" in_tail true
expect_out "the refusal says what to set" "dev: set ADK_NIX_GDS_TO_KICAD_ROOT to a gds_to_kicad checkout"

mkdir -p "$work/not-g2k"
T_G2K="$work/not-g2k" expect 1 "a gds_to_kicad root without pin_list.py refuses" in_tail true
expect_out "the refusal names the file" "no pin_list.py under $work/not-g2k\$"

mkdir -p "$work/not-a-plugin"
T_PLUGIN="$work/not-a-plugin" expect 1 "a plugin root without the export CLI refuses" in_tail true
expect_out "the refusal names the file" "no plugins/chiplet_export/chiplet_export_cli.py under"

mkdir -p "$work/host/bin"
printf '#!/bin/sh\nexit 0\n' > "$work/host/bin/adk-drc"
chmod +x "$work/host/bin/adk-drc"
T_PATH="$work/host/bin:$base_path" expect 1 "a host command of the same name first on PATH refuses" in_tail true
expect_out "the refusal names both" "adk-drc resolves to $work/host/bin/adk-drc, not $wrappers/bin/adk-drc"

T_PATH="$PATH" expect 1 "no wrappers on PATH refuses" in_tail true

# Anywhere on the host's PATH, a kicad-cli comes before the fork's.
mkdir -p "$work/host-kicad/bin"
printf '#!/bin/sh\nexit 0\n' > "$work/host-kicad/bin/kicad-cli"
chmod +x "$work/host-kicad/bin/kicad-cli"
T_PATH="$base_path:$work/host-kicad/bin" expect 1 "a host kicad-cli, even last on PATH, refuses" in_tail true
expect_out "the refusal names both" "kicad-cli resolves to $work/host-kicad/bin/kicad-cli, not $fork/bin/kicad-cli\$"

no_cli="$store/$(h32 8)-kicad-base-5d12a25667"
mkdir -p "$no_cli/lib"
T_FORK="$no_cli" expect 1 "a fork without kicad-cli refuses" in_tail true
expect_out "the refusal names the file" "no $no_cli/bin/kicad-cli\$"

T_MODE=evidence expect 1 "evidence mode with a stand-in store refuses" in_tail true
expect_out "the refusal says why" "evidence: ADK_NIX_STORE_DIR must be /nix/store"

# --- evidence mode's plugin tree -------------------------------------------

# A plugin repository with two commits, and an adk-tools checkout whose
# gitlink at tools/chiplet_kicad_plugin is the first; the plugin checkout sits
# at that path.
up="$work/up/plugin"
git init -q "$up"
mkdir -p "$up/plugins/chiplet_export"
echo one > "$up/plugins/chiplet_export/chiplet_export_cli.py"
g "$up" add -A && g "$up" commit -q -m one
rev="$(g "$up" rev-parse HEAD)"
echo two > "$up/plugins/chiplet_export/chiplet_export_cli.py"
g "$up" commit -q -am two
later="$(g "$up" rev-parse HEAD)"
tools="$work/adk-tools"
git init -q "$tools"
echo x > "$tools/README"
g "$tools" add -A
g "$tools" update-index --add --cacheinfo "160000,$rev,tools/chiplet_kicad_plugin"
g "$tools" commit -q -m base
# What `git clone` of adk-tools leaves before `submodule update --init`.
mkdir -p "$tools/tools/chiplet_kicad_plugin"

# plugin_check: the hook's plugin function alone, in evidence mode.
plugin_check() {
    env ADK_NIX_LIB="$lib" ADK_NIX_MODE=evidence ADK_NIX_ADK_TOOLS="$tools" bash -c '
ADK_NIX_LIB_ONLY=1 . "$ADK_NIX_LIB/cli-env.sh"
ADK_NIX_LIB_ONLY=1
. '"$here"'/tail-env.sh
adk_nix_tail_plugin'
}

expect 1 "evidence: an uninitialised plugin submodule refuses" plugin_check
expect_out "the refusal names the missing tree, not a commit" \
    "chiplet_kicad_plugin: no plugins/chiplet_export/chiplet_export_cli.py under $tools/tools/chiplet_kicad_plugin; initialise the tools/chiplet_kicad_plugin submodule\$"

rmdir "$tools/tools/chiplet_kicad_plugin"
git clone -q "$up" "$tools/tools/chiplet_kicad_plugin"
g "$tools/tools/chiplet_kicad_plugin" checkout -q --detach "$rev"

expect 0 "evidence: the plugin at its gitlink, clean, passes" plugin_check
expect_out "and resolves to tools/chiplet_kicad_plugin" "^$tools/tools/chiplet_kicad_plugin\$"

g "$tools/tools/chiplet_kicad_plugin" checkout -q --detach "$later"
expect 1 "evidence: the plugin at another commit refuses" plugin_check
expect_out "the refusal names both" "is at $later, pinned $rev"
g "$tools/tools/chiplet_kicad_plugin" checkout -q --detach "$rev"

echo changed >> "$tools/tools/chiplet_kicad_plugin/plugins/chiplet_export/chiplet_export_cli.py"
expect 1 "evidence: a plugin with changes refuses" plugin_check
g "$tools/tools/chiplet_kicad_plugin" checkout -q -- .

touch "$tools/tools/chiplet_kicad_plugin/untracked.py"
expect 1 "evidence: a plugin with an untracked file refuses" plugin_check
rm "$tools/tools/chiplet_kicad_plugin/untracked.py"

expect 0 "evidence: restored, it passes again" plugin_check

# --- evidence mode's gds_to_kicad tree -------------------------------------

# The same layout at tools/gds_to_kicad, in an adk-tools checkout of its own.
gup="$work/up/gds_to_kicad"
git init -q "$gup"
echo one > "$gup/pin_list.py"
g "$gup" add -A && g "$gup" commit -q -m one
grev="$(g "$gup" rev-parse HEAD)"
echo two >> "$gup/pin_list.py"
g "$gup" commit -q -am two
glater="$(g "$gup" rev-parse HEAD)"
gtools="$work/adk-tools-g2k"
git init -q "$gtools"
echo x > "$gtools/README"
g "$gtools" add -A
g "$gtools" update-index --add --cacheinfo "160000,$grev,tools/gds_to_kicad"
g "$gtools" commit -q -m base
# What `git clone` of adk-tools leaves before `submodule update --init`.
mkdir -p "$gtools/tools/gds_to_kicad"

g2k_check() {
    env ADK_NIX_LIB="$lib" ADK_NIX_MODE=evidence ADK_NIX_ADK_TOOLS="$gtools" bash -c '
ADK_NIX_LIB_ONLY=1 . "$ADK_NIX_LIB/cli-env.sh"
ADK_NIX_LIB_ONLY=1
. '"$here"'/tail-env.sh
adk_nix_tail_gds_to_kicad'
}

expect 1 "evidence: an uninitialised gds_to_kicad submodule refuses" g2k_check
expect_out "the refusal names the missing tree, not a commit" \
    "gds_to_kicad: no pin_list.py under $gtools/tools/gds_to_kicad; initialise the tools/gds_to_kicad submodule\$"

rmdir "$gtools/tools/gds_to_kicad"
git clone -q "$gup" "$gtools/tools/gds_to_kicad"
g "$gtools/tools/gds_to_kicad" checkout -q --detach "$grev"
expect 0 "evidence: gds_to_kicad at its gitlink, clean, passes" g2k_check
expect_out "and resolves to tools/gds_to_kicad" "^$gtools/tools/gds_to_kicad\$"

g "$gtools/tools/gds_to_kicad" checkout -q --detach "$glater"
expect 1 "evidence: gds_to_kicad at another commit refuses" g2k_check
expect_out "the refusal names both" "is at $glater, pinned $grev"
g "$gtools/tools/gds_to_kicad" checkout -q --detach "$grev"

echo changed >> "$gtools/tools/gds_to_kicad/pin_list.py"
expect 1 "evidence: gds_to_kicad with changes refuses" g2k_check
g "$gtools/tools/gds_to_kicad" checkout -q -- .

expect 0 "evidence: restored, it passes again" g2k_check

echo "tail-env tests: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
