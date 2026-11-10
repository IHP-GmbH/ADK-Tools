#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The KLayout isolation of devShells.cli, with the real klayout: a host home
# that registers a technology named SG13_dev (as ~/.klayout does on a host with
# ihp-sg13cmos5l installed) must not reach `klayout -b` or `klayout -zz` inside
# the shell, and the interposer technology KLAYOUT_PATH names must.
#
# Run inside the shell (dev or evidence), after the hook: it reads the hook's
# KLAYOUT_HOME and KLAYOUT_PATH. The controls run the same klayout with the
# host home in reach, so an absent SG13_dev in the shell is shown to be the
# isolation's doing and not a probe that sees nothing. Writes only under one
# mktemp directory.
#
# Exit: 0 all passed, 1 a check failed, 2 cannot run here.
#
# Usage: nix/test-klayout-home.sh

set -uo pipefail

if [ -z "${ADK_NIX_KLAYOUT:-}" ] || [ -z "${KLAYOUT_HOME:-}" ] || [ -z "${KLAYOUT_PATH:-}" ] \
        || [ -z "${INTERPOSER_PDK_ROOT:-}" ]; then
    echo "test-klayout-home: run inside devShells.cli, after the hook" >&2
    exit 2
fi
klayout="$(command -v klayout)" || { echo "test-klayout-home: no klayout on PATH" >&2; exit 2; }
if [ "$(realpath "$klayout")" != "$(realpath "$ADK_NIX_KLAYOUT/bin/klayout")" ]; then
    echo "test-klayout-home: klayout on PATH is $klayout, not the shell's" >&2
    exit 2
fi
# The interposer technology's name, from its own file, so the positive check
# does not depend on a name typed here.
lyt="$(ls "$INTERPOSER_PDK_ROOT"/libs.tech/klayout/tech/*.lyt 2>/dev/null | head -n 1)"
pdk_tech="$(sed -n 's|.*<name>\(.*\)</name>.*|\1|p' "$lyt" 2>/dev/null | head -n 1)"
if [ -z "$pdk_tech" ]; then
    echo "test-klayout-home: no technology name in $INTERPOSER_PDK_ROOT/libs.tech/klayout/tech/*.lyt" >&2
    exit 2
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-nix-klayout-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# A host home as a host with ihp-sg13cmos5l installed has it: a technology
# folder under ~/.klayout/tech whose .lyt is named SG13_dev.
host="$work/host-home"
mkdir -p "$host/.klayout/tech/rogue"
cat > "$host/.klayout/tech/rogue/rogue.lyt" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<technology>
 <name>SG13_dev</name>
 <description>host technology the shell must not see</description>
 <dbu>0.001</dbu>
</technology>
EOF
cat > "$work/probe.py" <<'EOF'
import pya
print("TECHS " + "|".join(sorted(pya.Technology.technology_names())))
EOF

passed=0
failed=0

# techs <mode> <env args...>: the technology names klayout registers, run in
# <mode> (-b or -zz) under `env <env args>` (unsets first, as env wants them),
# from the host home's parent.
techs() {
    local mode="$1"
    shift
    (cd "$work" && env "$@" "$klayout" "$mode" -r "$work/probe.py" 2>"$work/probe.err") \
        | sed -n 's/^TECHS //p'
}

# has <description> <0|1> <name> <names>: whether <name> is among <names>.
has() {
    local what="$1" want="$2" name="$3" names="$4" got=1
    [[ "|$names|" == *"|$name|"* ]] && got=0
    if [ "$got" -eq "$want" ]; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $what (registered: ${names:-<none>})"
        sed 's/^/    /' "$work/probe.err"
    fi
}

for mode in -b -zz; do
    # Controls: the host home in reach, the way KLayout finds it outside the
    # shell (HOME alone), and through each variable the hook sets.
    names="$(techs "$mode" -u KLAYOUT_HOME -u KLAYOUT_PATH HOME="$host")"
    has "control $mode: HOME alone reaches the host SG13_dev" 0 SG13_dev "$names"
    names="$(techs "$mode" -u KLAYOUT_PATH HOME="$host" KLAYOUT_HOME="$host/.klayout")"
    has "control $mode: KLAYOUT_HOME at the host home reaches it" 0 SG13_dev "$names"
    names="$(techs "$mode" HOME="$host" KLAYOUT_PATH="$host/.klayout:$KLAYOUT_PATH")"
    has "control $mode: KLAYOUT_PATH through the host home reaches it" 0 SG13_dev "$names"

    # The shell as the hook left it, with the host home as HOME.
    names="$(techs "$mode" HOME="$host")"
    has "shell $mode: the host SG13_dev does not reach klayout" 1 SG13_dev "$names"
    has "shell $mode: the interposer technology $pdk_tech does" 0 "$pdk_tech" "$names"
done

echo "klayout-home: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
