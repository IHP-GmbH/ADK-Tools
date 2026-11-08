#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The tool identity table in the image (nix/tool_identity.py): reads the
# version each tool no pin names reports, then runs `image` (write the table) or
# `verify` (refuse a written table other than the one this image gives), with
# the facts build.sh wrote from the build host's store. The image's tool paths
# are named here once, so the runtime step that writes the table and the verify
# step that checks it read the same files.
#
# Chiplet Studio prints no version; its entry's version is the commit
# manifest.json records for it. So are the two DRC runners': adk-drc, the
# wrapper that runs the ADK's run_drc.py, keyed with the whole ADK tree (its
# deck, registry and rule parameters), and the interposer PDK's run_drc.py,
# keyed with its drc folder. That script has no #! line; packaging-flow runs
# it under the venv's python, the pad-map interpreter. The carrier DRC gate
# around that script, adk-verify-carrier-drc, is one file of this repository
# with no tree and no release of its own: its version is the sha256 of its
# bytes, so it moves only when the script does. chiplet-cosim and
# assembly-lvs run from their staged checkouts, so tool_identity.py computes each one's version from its
# checkout: the commit HEAD names and the digest of every file its index tracks. The python row's tree is
# the venv: its interpreter is the system's by realpath, so what is installed
# into the venv is what tells it apart. A version run writes
# into a scratch home, which goes again, so nothing a table names moves.
#
# usage: tool-identity-image.sh image --out FILE
#        tool-identity-image.sh verify --table FILE [--schema FILE]
set -euo pipefail

ADK=/opt/adk-tools
PY="$ADK/venv/bin/python3"
mode="${1:?usage: tool-identity-image.sh image|verify [options]}"
shift

home="$(mktemp -d)"
kicad="$(HOME="$home" QT_QPA_PLATFORM=offscreen kicad-cli version)"
klayout="$(HOME="$home" KLAYOUT_HOME="$home/klayout" klayout -v)"
rm -rf "$home"
read -r kmodule kdir kbinary < <("$PY" -B -c '
import os, klayout, klayout.dbcore as m
print(klayout.__version__, os.path.dirname(klayout.__file__), m.__file__)')
ref() { "$PY" -B -c "import json, sys; print(json.load(open('$ADK/manifest.json'))['tools'][sys.argv[1]]['ref'])" "$1"; }
studio="commit $(ref chiplet-studio)"
adk="commit $(ref adk)"
intm4tm2="commit $(ref OpenIntM4TM2)"
carrier="sha256 $(sha256sum < "$(readlink -f /usr/local/bin/adk-verify-carrier-drc)" | cut -d' ' -f1)"

exec "$PY" -B "$ADK/nix/tool_identity.py" "$mode" \
    --facts "$ADK/nix/tool-identity.facts.json" \
    --tool "adk-drc=$adk:/usr/local/bin/adk-drc:$ADK/adk" \
    --tool "adk-verify-carrier-drc=$carrier:/usr/local/bin/adk-verify-carrier-drc" \
    --tool "chiplet-studio=$studio:$ADK/chiplet-studio/build/chiplet-studio:$ADK/chiplet-studio/build" \
    --tool "intm4tm2-run-drc=$intm4tm2:$ADK/OpenIntM4TM2/libs.tech/klayout/tech/drc/run_drc.py:$ADK/OpenIntM4TM2/libs.tech/klayout/tech/drc" \
    --tool "kicad-cli=$kicad:/usr/bin/kicad-cli" \
    --tool "klayout=$klayout:$ADK/chiplet-studio/extern/klayout/bin-release/klayout:$ADK/chiplet-studio/extern/klayout/bin-release" \
    --tool "klayout-python=$kmodule:$kbinary:$kdir" \
    --tool "python=$("$PY" -V):$PY:$ADK/venv" \
    --tool "git=$(/usr/bin/git --version):/usr/bin/git" \
    --tracked-tool "chiplet-cosim=$ADK/venv/bin/chiplet-cosim:$ADK/staging/chiplet-cosim" \
    --tracked-tool "assembly-lvs=$ADK/venv/bin/assembly-lvs:$ADK/staging/assembly-lvs" \
    "$@"
