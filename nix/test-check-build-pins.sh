#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for nix/check-build-pins.sh, without Nix. The check runs against a
# stand-in adk-tools checkout (staging.pin, Dockerfile, flake.lock, two
# patches whose order matters), throwaway IHP-Open-PDK and LibreLane
# repositories, and a stand-in `nix` on PATH that prints a canned derivation
# graph and a canned "realised" source tree. Each refusal is reached by
# breaking exactly one thing against a setup that passes.
#
# Needs bash, git, python3 and coreutils. Writes only under one mktemp
# directory.
#
# Usage: nix/test-check-build-pins.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
check="$here/check-build-pins.sh"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-pins-test.XXXXXX")"
trap 'chmod -R u+w "$work" 2>/dev/null; rm -rf "$work"' EXIT
work="$(realpath "$work")"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
export ADK_NIX_STORE_DIR="$work/store"
export TMPDIR="$work/tmp"
mkdir -p "$TMPDIR" "$ADK_NIX_STORE_DIR"

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

# expect_no_out <description> <pattern>: no line of the last output matches.
expect_no_out() {
    if ! grep -q -E -- "$2" "$work/last.out"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $1 (a line matches $2)"
        sed 's/^/    /' "$work/last.out"
    fi
}

# expect_last <description> <pattern>: the last command's last line matches.
expect_last() {
    if tail -n1 "$work/last.out" | grep -q -E -- "$2"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $1 (last line does not match $2)"
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

h32() { printf '%032d' "$1" | tr 0-9 a-j; }
g() { git -C "$@"; }

# --- throwaway upstreams ---------------------------------------------------

# Two submodule repositories, each with one commit.
for sub in pycell4klayout-api pypreprocessor; do
    git init -q "$work/up/$sub"
    echo "$sub" > "$work/up/$sub/README"
    g "$work/up/$sub" add -A && g "$work/up/$sub" commit -q -m init
done
pycell="$(g "$work/up/pycell4klayout-api" rev-parse HEAD)"
pypre="$(g "$work/up/pypreprocessor" rev-parse HEAD)"

# The parent PDK: .gitmodules plus the two gitlinks, and a later commit that
# moves one gitlink, so "the gitlink at SG13G2_REF" is not "the latest".
pdk="$work/up/IHP-Open-PDK"
git init -q "$pdk"
cat > "$pdk/.gitmodules" <<'EOF'
[submodule "ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api"]
	path = ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api
	url = https://github.com/IHP-GmbH/pycell4klayout-api
[submodule "ihp-sg13g2/libs.tech/klayout/python/pypreprocessor"]
	path = ihp-sg13g2/libs.tech/klayout/python/pypreprocessor
	url = https://github.com/IHP-GmbH/pypreprocessor.git
EOF
g "$pdk" add .gitmodules
g "$pdk" update-index --add --cacheinfo "160000,$pycell,ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api"
g "$pdk" update-index --add --cacheinfo "160000,$pypre,ihp-sg13g2/libs.tech/klayout/python/pypreprocessor"
g "$pdk" commit -q -m parent
sg13g2="$(g "$pdk" rev-parse HEAD)"
g "$pdk" update-index --cacheinfo "160000,$pypre,ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api"
g "$pdk" commit -q -m "move a gitlink"

# LibreLane: one commit with a submodule path, and two patches that only
# apply in order (the second edits a line the first adds).
ll="$work/up/librelane"
git init -q "$ll"
mkdir -p "$ll/librelane/scripts"
printf 'one\n' > "$ll/librelane/scripts/ys_common.py"
printf '#!/bin/sh\n' > "$ll/run.sh"
chmod +x "$ll/run.sh"
ln -s librelane/scripts "$ll/scripts"
g "$ll" add -A
g "$ll" update-index --add --cacheinfo "160000,$pycell,test/designs"
g "$ll" commit -q -m upstream
llrev="$(g "$ll" rev-parse HEAD)"
at="$work/adk-tools"
mkdir -p "$at/patches"
cp -r "$ll" "$work/scratch"
printf 'one\ntwo\n' > "$work/scratch/librelane/scripts/ys_common.py"
g "$work/scratch" diff > "$at/patches/a.patch"
g "$work/scratch" commit -q -am a
printf 'one\nTWO\n' > "$work/scratch/librelane/scripts/ys_common.py"
g "$work/scratch" diff > "$at/patches/b.patch"
g "$work/scratch" commit -q -am b
patched_tree="$(g "$work/scratch" rev-parse HEAD^{tree})"
# The commit an earlier recipe would have named: the patched tree, committed
# with some message, fetched into the LibreLane clone.
g "$ll" fetch -q "$work/scratch" HEAD
patched="$(printf 'patched\n' | g "$ll" commit-tree -p "$llrev" "$patched_tree")"

openroad="$ADK_NIX_STORE_DIR/$(h32 1)-openroad-2026-02-17"

# --- the stand-in checkout -------------------------------------------------

write_pin() {
    cat > "$at/staging.pin" <<EOF
LIBRELANE_REPO=https://github.com/librelane/librelane.git
LIBRELANE_REV=$llrev
LIBRELANE_PATCHES="${1:-patches/a.patch patches/b.patch}"
LIBRELANE_PATCHED_REV=${2:-$patched}
NIX_OPENROAD_OUTPATH="$openroad"
EOF
}
write_dockerfile() { printf 'FROM x\nARG SG13G2_REF=%s\nARG PYCELL_API_REF=%s\n' "${1:-$sg13g2}" "$pypre" > "$at/Dockerfile"; }
# write_lock [<input> <rev> [<owner/repo>]]: the lock, with one input changed.
write_lock() {
    python3 - "$at/flake.lock" "$llrev" "$sg13g2" "$pycell" "$pypre" "$@" <<'EOF'
import json, sys
out, ll, pdk, pycell, pypre = sys.argv[1:6]
inputs = {"librelane": (ll, "librelane/librelane"), "ihp-open-pdk": (pdk, "IHP-GmbH/IHP-Open-PDK"),
          "pycell4klayout-api": (pycell, "IHP-GmbH/pycell4klayout-api"),
          "pypreprocessor": (pypre, "IHP-GmbH/pypreprocessor")}
extra = sys.argv[6:]
if extra:
    name = extra[0]
    if len(extra) == 1:
        inputs.pop(name)
    else:
        inputs[name] = (extra[1], extra[2] if len(extra) > 2 else inputs[name][1])
nodes = {"root": {"inputs": {k: k + "_2" for k in inputs}}}
for k, (rev, repo) in inputs.items():
    owner, name = repo.split("/")
    nodes[k + "_2"] = {"locked": {"type": "github", "owner": owner, "repo": name, "rev": rev,
                                  "narHash": "sha256-x"}, "original": {}}
json.dump({"nodes": nodes, "root": "root", "version": 7}, open(out, "w"))
EOF
}

# The derivation graph the stand-in nix prints: LibreLane, the pinned
# OpenROAD, and optionally a second derivation named like it.
write_graph() {
    python3 - "$work/graph.json" "$openroad" "$@" <<'EOF'
import json, sys
out, openroad = sys.argv[1:3]
graph = {"/x/librelane.drv": {"name": "python3.13-librelane-3.0.0rc0",
                              "outputs": {"out": {"path": "/x/librelane"}}}}
if "no-openroad" not in sys.argv[3:]:
    graph["/x/openroad.drv"] = {"name": "openroad-2026-02-17", "outputs": {"out": {"path": openroad}}}
if "second" in sys.argv[3:]:
    graph["/x/openroad2.drv"] = {"name": "openroad-2026-02-17",
                                 "outputs": {"out": {"path": openroad + "-other"}}}
json.dump(graph, open(out, "w"))
EOF
}

# The "realised" patched source: the patched tree without .git or the
# submodule path, as the flake input and applyPatches leave it.
src="$ADK_NIX_STORE_DIR/$(h32 2)-librelane-359bedc-patched"
write_src() {
    chmod -R u+w "$src" 2>/dev/null
    rm -rf "$src"
    mkdir -p "$src"
    g "$work/scratch" archive HEAD | tar -x -C "$src"
    rm -rf "$src/test"
}

mkdir -p "$work/bin"
cat > "$work/bin/nix" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$work/nix.log"
case "\$1" in
    derivation) cat "$work/graph.json" ;;
    build) echo "\${STUB_SRC:-$src}" ;;
    *) exit 2 ;;
esac
EOF
chmod +x "$work/bin/nix"
export PATH="$work/bin:$PATH"

setup() { write_pin; write_dockerfile; write_lock; write_graph; write_src; : > "$work/nix.log"; }
run() { "$check" --pdk-git "$pdk" "$@" "$at"; }
run_all() { run --realize --librelane-git "$ll"; }

# --- a setup that passes ---------------------------------------------------

setup
expect 0 "all pins agree, without --realize" run
expect_last "... and ends naming the patched source and commit UNVERIFIED" \
    "^adk-nix: pins: checked lock, openroad; UNVERIFIED patched source, LIBRELANE_PATCHED_REV $patched \\(no --realize\\)$"
expect 0 "the eval passes no-update-lock-file, no IFD, max-jobs 0" \
    grep -q -- "derivation show --recursive --max-jobs 0 --no-update-lock-file --option allow-import-from-derivation false $at#packages.x86_64-linux.librelane" "$work/nix.log"
expect 0 "all pins agree, with --realize" run_all
expect_out "... the patched tree compared" "is LIBRELANE_REV with LIBRELANE_PATCHES applied"
expect_out "... and LIBRELANE_PATCHED_REV's tree too" "LIBRELANE_PATCHED_REV $patched has that tree"
expect_last "... and ends naming all four checked, none unverified" \
    "^adk-nix: pins: checked lock, openroad, patched source, LIBRELANE_PATCHED_REV $patched$"

# --- arguments ---------------------------------------------------------------

expect 1 "no --pdk-git" "$check" "$at"
expect 1 "--realize without --librelane-git" "$check" --pdk-git "$pdk" --realize "$at"
expect 1 "an unknown option" "$check" --pdk-git "$pdk" --nope "$at"

# --- lock ------------------------------------------------------------------

setup; write_lock librelane "$(h32 3 | tr a-j 0-9)00000000"
expect 1 "lock librelane is not LIBRELANE_REV" run
setup; write_lock ihp-open-pdk "$(g "$pdk" rev-parse HEAD)"
expect 1 "lock parent is a later commit, not SG13G2_REF" run
setup; write_lock ihp-open-pdk "$sg13g2" someone/IHP-Open-PDK
expect 1 "lock parent from another repository" run
setup; write_lock pycell4klayout-api "$pypre"
expect 1 "lock submodule at the later parent's gitlink" run
setup; write_lock pypreprocessor "$pypre" someone/pypreprocessor
expect 1 "lock submodule from another repository" run
setup; write_lock pypreprocessor
expect 1 "lock without a submodule input" run
setup; write_dockerfile "$(g "$pdk" rev-parse HEAD)"
expect 1 "Dockerfile names a commit the lock does not" run
setup; printf 'ARG SG13G2_REF=%s\nARG SG13G2_REF=%s\n' "$sg13g2" "$sg13g2" > "$at/Dockerfile"
expect 1 "two SG13G2_REF lines" run
setup; printf 'LIBRELANE_REV=%s\n' "$llrev" >> "$at/staging.pin"
expect 1 "two LIBRELANE_REV lines" run
setup; printf 'LIBRELANE_REV=deadbeef\n' >> "$at/staging.pin"
expect 1 "a pin a shell reads differently from its literal" run
setup; printf 'LIBRELANE_PATCHED_REV=deadbeef\n' >> "$at/staging.pin"
expect 1 "LIBRELANE_PATCHED_REV read differently by a shell" run
setup
expect 1 "a PDK clone without SG13G2_REF" "$check" --pdk-git "$work/up/pypreprocessor" "$at"

# --- openroad --------------------------------------------------------------

setup; write_graph no-openroad
expect 1 "LibreLane's graph does not build the pinned OpenROAD" run
expect_out "... refused by name, not by a crash" "graph does not build $openroad"
setup; write_graph second
expect 1 "LibreLane's graph builds a second OpenROAD" run
setup; sed -i "s|^NIX_OPENROAD_OUTPATH=.*|NIX_OPENROAD_OUTPATH=\"/elsewhere/$(h32 1)-openroad\"|" "$at/staging.pin"
expect 1 "an OpenROAD pin outside the store" run

# --- patched ---------------------------------------------------------------

setup; printf 'one\nTWO\nthree\n' > "$src/librelane/scripts/ys_common.py"
expect 1 "realised source with a changed file" run_all
setup; touch "$src/extra"
expect 1 "realised source with an extra file" run_all
setup; rm "$src/run.sh"
expect 1 "realised source missing a file" run_all
setup; chmod -x "$src/run.sh"
expect 1 "realised source with an executable bit lost" run_all
setup; rm "$src/scripts" && ln -s librelane "$src/scripts"
expect 1 "realised source with a symlink retargeted" run_all
setup; mkdir -p "$src/test/designs" && touch "$src/test/designs/x"
expect 0 "files under a submodule path are not compared" run_all
setup; write_pin "patches/b.patch patches/a.patch"
expect 1 "patches in the wrong order do not apply" run_all
setup; write_pin "patches/a.patch"
expect 1 "one patch fewer: the realised tree differs" run_all
setup; write_pin "patches/a.patch patches/b.patch patches/missing.patch"
expect 1 "a patch that is not a file" run_all
setup; write_pin "" "$llrev"
expect 1 "LIBRELANE_PATCHED_REV whose tree is not the patched one" run_all
setup; write_pin "" "$(h32 4 | tr a-j 0-9)00000000"
expect 0 "LIBRELANE_PATCHED_REV absent from the clone" run_all
expect_last "... and ends naming that commit UNVERIFIED" \
    "^adk-nix: pins: checked lock, openroad, patched source; UNVERIFIED LIBRELANE_PATCHED_REV [0-9a-f]+ \\(not in "
expect_no_out "... and never says its tree was compared" "LIBRELANE_PATCHED_REV [0-9a-f]+ has that tree"
setup
expect 1 "a realised path outside the store" env STUB_SRC="$work/not-store" "$check" --pdk-git "$pdk" --realize --librelane-git "$ll" "$at"
setup
expect 1 "a LibreLane clone without LIBRELANE_REV" "$check" --pdk-git "$pdk" --realize --librelane-git "$work/up/pypreprocessor" "$at"

echo "check-build-pins: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
