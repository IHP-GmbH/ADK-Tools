#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for nix/build-env.sh, without Nix: the functions are sourced on their
# own (ADK_NIX_LIB_ONLY=1) and run against a stand-in store under one mktemp
# directory, whose LibreLane "wrapper" is a bash script with the first line
# makeWrapper writes and which prints what it was started with. The launcher
# is run for real, so what it passes on and what it drops is observed, not
# read from its text.
#
# Needs bash and coreutils. Writes only under one mktemp directory.
#
# Usage: nix/test-build-env.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-nix-build-test.XXXXXX")"
trap 'chmod -R u+w "$work" 2>/dev/null; rm -rf "$work"' EXIT
work="$(realpath "$work")"
export ADK_NIX_STORE_DIR="$work/store"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/cli-env.sh"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/build-env.sh"

passed=0
failed=0

# expect <0|1> <description> <command...>: the command's exit status, run in a
# subshell because the checks exit rather than return.
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

# expect_out <description> <pattern> / expect_no_out: whether the last
# expect's output has a line matching the pattern.
expect_out() {
    if grep -q -E -- "$2" "$work/last.out"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $1 (no line matching $2)"
        sed 's/^/    /' "$work/last.out"
    fi
}
expect_no_out() {
    if ! grep -q -E -- "$2" "$work/last.out"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $1 (a line matches $2)"
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

h32() { printf '%032d' "$1" | tr 0-9 a-j; }

# The stand-in store: a bash, and a LibreLane whose wrapper reports its
# environment and arguments one per line.
mkdir -p "$ADK_NIX_STORE_DIR"
bash_pkg="$ADK_NIX_STORE_DIR/$(h32 1)-bash-5.3p3"
mkdir -p "$bash_pkg/bin"
ln -s "$(command -v bash)" "$bash_pkg/bin/bash"
librelane="$ADK_NIX_STORE_DIR/$(h32 2)-python3.13-librelane-3.0.0rc0"
mkdir -p "$librelane/bin"
cat > "$librelane/bin/librelane" <<EOF
#! $bash_pkg/bin/bash -e
echo "PYTHONPATH=\${PYTHONPATH-<unset>}"
echo "PYTHONHOME=\${PYTHONHOME-<unset>}"
echo "PYTHONSTARTUP=\${PYTHONSTARTUP-<unset>}"
echo "PYTHONNOUSERSITE=\${PYTHONNOUSERSITE-<unset>}"
echo "PYTHONDONTWRITEBYTECODE=\${PYTHONDONTWRITEBYTECODE-<unset>}"
echo "PYTHONVERBOSE=\${PYTHONVERBOSE-<unset>}"
echo "PYTHONOPTIMIZE=\${PYTHONOPTIMIZE-<unset>}"
echo "PYTHON_CPU_COUNT=\${PYTHON_CPU_COUNT-<unset>}"
echo "NIX_PYTHONPATH=\${NIX_PYTHONPATH-<unset>}"
echo "NIX_PYTHONEXECUTABLE=\${NIX_PYTHONEXECUTABLE-<unset>}"
echo "NIX_PYTHONPREFIX=\${NIX_PYTHONPREFIX-<unset>}"
echo "PDK_ROOT=\${PDK_ROOT-<unset>}"
echo "BASH_ENV=\${BASH_ENV-<unset>}"
echo "ENV=\${ENV-<unset>}"
for a in "\$@"; do echo "arg=\$a"; done
EOF
chmod 0555 "$librelane/bin/librelane"

# Checked trees, as cli-env.sh resolves them: chiplet-spec once per directory
# pip installs from.
cf="$work/src/chiplet-flow"
sp="$work/src/chiplet-spec"
mkdir -p "$cf/src/chiplet_die_flow" "$cf/src/librelane_plugin_chiplet" \
         "$sp/reference/python/chiplet_format_io" "$sp/description/src/chiplet_system_description"
touch "$cf/src/chiplet_die_flow/__init__.py" "$cf/src/librelane_plugin_chiplet/__init__.py" \
      "$sp/reference/python/chiplet_format_io/__init__.py" \
      "$sp/description/src/chiplet_system_description/__init__.py"
trees="chiplet-flow|$cf|.
interposer-pnr|$work/src/interposer-pnr|.
chiplet-spec|$sp|reference/python
chiplet-spec|$sp|description"

pdk="$ADK_NIX_STORE_DIR/$(h32 3)-ihp-open-pdk-22f4335"
mkdir -p "$pdk/ihp-sg13g2/libs.tech/librelane"

# --- paths it will and will not write down
expect 0 "plain path" adk_nix_plain_path /a/b-c/d.e_f+g@h
expect 1 "relative path" adk_nix_plain_path a/b
expect 1 "path with a colon" adk_nix_plain_path /a:b
expect 1 "path with a space" adk_nix_plain_path "/a b"
expect 1 "path with a quote" adk_nix_plain_path "/a'b"
expect 1 "path with a dot-dot" adk_nix_plain_path /a/../b
expect 1 "path with a dot" adk_nix_plain_path /a/./b
expect 1 "path with a double slash" adk_nix_plain_path /a//b
expect 0 "store path" adk_nix_store_path "$librelane"
expect 1 "below a store path" adk_nix_store_path "$librelane/bin"
expect 1 "short hash" adk_nix_store_path "$ADK_NIX_STORE_DIR/abc-librelane"
expect 1 "outside the store" adk_nix_store_path "$work/$(h32 2)-librelane"

# --- the wrapper's bash
expect_eq "the wrapper's bash" "$(adk_nix_wrapper_bash "$librelane/bin/librelane")" "$bash_pkg/bin/bash"
other="$ADK_NIX_STORE_DIR/$(h32 4)-python3.13-librelane-x"
mkdir -p "$other/bin"
printf '#!/bin/sh\n' > "$other/bin/librelane"
chmod 0555 "$other/bin/librelane"
expect 1 "a wrapper on /bin/sh" adk_nix_wrapper_bash "$other/bin/librelane"
chmod u+w "$other/bin/librelane"
printf '#! %s/%s-python3-3.13/bin/bash -e\n' "$ADK_NIX_STORE_DIR" "$(h32 5)" > "$other/bin/librelane"
expect 1 "a wrapper whose bash is not a bash package" adk_nix_wrapper_bash "$other/bin/librelane"
printf '#! %s/bin/bash\n' "$bash_pkg" > "$other/bin/librelane"
expect 1 "a wrapper without -e" adk_nix_wrapper_bash "$other/bin/librelane"

# --- the launcher's text
text="$(adk_nix_launcher_text "$librelane" "$cf/src" "$sp/reference/python")"
expect_eq "first line is the wrapper's bash, privileged" "$(head -n1 <<< "$text")" "#!$bash_pkg/bin/bash -p"
expect_eq "PYTHONPATH is the roots, in order" "$(grep '^export PYTHONPATH=' <<< "$text")" \
    "export PYTHONPATH='$cf/src:$sp/reference/python'"
expect_eq "it runs the wrapper under the same bash, -p -e" "$(tail -n1 <<< "$text")" \
    "exec '$bash_pkg/bin/bash' -p -e '$librelane/bin/librelane' \"\$@\""
expect 1 "not a store path" adk_nix_launcher_text "$work/librelane" "$cf/src"
expect 1 "no bin/librelane" adk_nix_launcher_text "$bash_pkg" "$cf/src"
expect 1 "a wrapper it does not recognise" adk_nix_launcher_text "$other" "$cf/src"
expect 1 "no roots" adk_nix_launcher_text "$librelane"
expect 1 "a relative root" adk_nix_launcher_text "$librelane" src
expect 0 "a root of another filesystem is written down" adk_nix_launcher_text "$librelane" /opt/elsewhere/src
mkdir -p "$work/with:colon"
expect 1 "a root with a colon" adk_nix_launcher_text "$librelane" "$work/with:colon"
expect 1 "a root named twice" adk_nix_launcher_text "$librelane" "$cf/src" "$sp/reference/python" "$cf/src"

# --- writing it
dir="$work/state/launcher"
sha="$(adk_nix_write_launcher "$dir" "$librelane" "$cf/src" "$sp/reference/python")"
expect_eq "printed digest is the file's" "$sha" "$(sha256sum "$dir/librelane" | cut -d' ' -f1)"
expect_eq "file is the text" "$(cat "$dir/librelane")" "$text"
expect_eq "mode 0555" "$(stat -c %a "$dir/librelane")" 555
expect 0 "a regular file, not a link" test -f "$dir/librelane" -a ! -L "$dir/librelane"
expect_eq "nothing left beside it" "$(ls -A "$dir")" librelane
sha2="$(adk_nix_write_launcher "$dir" "$librelane" "$sp/reference/python")"
expect_eq "rewritten over the read-only one" "$sha2" "$(sha256sum "$dir/librelane" | cut -d' ' -f1)"
expect 1 "a directory it cannot write down" adk_nix_write_launcher "$work/a b" "$librelane" "$cf/src"
expect 1 "a refused text writes nothing" adk_nix_write_launcher "$work/state/refused" "$librelane" src
expect 1 "... and leaves no file" test -e "$work/state/refused/librelane"
expect 1 "a missing root" adk_nix_write_launcher "$work/state/missing" "$librelane" "$cf/src" "$work/nowhere"
expect_out "... refused for it" "source root $work/nowhere is not a directory"
expect 1 "... and leaves no file" test -e "$work/state/missing/librelane"

# --- running it: the roots and nothing ambient, the caller's arguments as given
adk_nix_write_launcher "$dir" "$librelane" "$cf/src" "$sp/reference/python" >/dev/null
out="$(env PYTHONPATH=/ambient PYTHONHOME=/elsewhere PYTHONSTARTUP=/x.py PYTHONVERBOSE=1 \
        PYTHONOPTIMIZE=2 PYTHON_CPU_COUNT=1 NIX_PYTHONPATH=/hostile NIX_PYTHONEXECUTABLE=/x/python \
        NIX_PYTHONPREFIX=/x PDK_ROOT=/kept "$dir/librelane" --manual-pdk "a b" 'c$d')"
expect_eq "PYTHONPATH replaced" "$(grep '^PYTHONPATH=' <<< "$out")" "PYTHONPATH=$cf/src:$sp/reference/python"
expect_eq "PYTHONHOME dropped" "$(grep '^PYTHONHOME=' <<< "$out")" "PYTHONHOME=<unset>"
expect_eq "PYTHONSTARTUP dropped" "$(grep '^PYTHONSTARTUP=' <<< "$out")" "PYTHONSTARTUP=<unset>"
expect_eq "no user site" "$(grep '^PYTHONNOUSERSITE=' <<< "$out")" "PYTHONNOUSERSITE=1"
expect_eq "no bytecode written" "$(grep '^PYTHONDONTWRITEBYTECODE=' <<< "$out")" "PYTHONDONTWRITEBYTECODE=1"
expect_eq "PYTHONVERBOSE passes, for the import-origin probe" "$(grep '^PYTHONVERBOSE=' <<< "$out")" "PYTHONVERBOSE=1"
expect_eq "PYTHONOPTIMIZE dropped" "$(grep '^PYTHONOPTIMIZE=' <<< "$out")" "PYTHONOPTIMIZE=<unset>"
expect_eq "PYTHON_CPU_COUNT dropped" "$(grep '^PYTHON_CPU_COUNT=' <<< "$out")" "PYTHON_CPU_COUNT=<unset>"
expect_eq "NIX_PYTHONPATH dropped" "$(grep '^NIX_PYTHONPATH=' <<< "$out")" "NIX_PYTHONPATH=<unset>"
expect_eq "NIX_PYTHONEXECUTABLE dropped" "$(grep '^NIX_PYTHONEXECUTABLE=' <<< "$out")" "NIX_PYTHONEXECUTABLE=<unset>"
expect_eq "NIX_PYTHONPREFIX dropped" "$(grep '^NIX_PYTHONPREFIX=' <<< "$out")" "NIX_PYTHONPREFIX=<unset>"
expect_eq "other variables pass" "$(grep '^PDK_ROOT=' <<< "$out")" "PDK_ROOT=/kept"
expect_eq "arguments as given" "$(grep '^arg=' <<< "$out")" "arg=--manual-pdk
arg=a b
arg=c\$d"

# What the caller's bash would source or import. BASH_ENV runs in any
# non-interactive bash, the wrapper's included, after the drop; an exported
# function can stand in for the builtins either script runs.
printf 'export PYTHONPATH=/ambient${PYTHONPATH:+:$PYTHONPATH} NIX_PYTHONPATH=/hostile\n' > "$work/bash_env"
out="$(env BASH_ENV="$work/bash_env" ENV="$work/bash_env" "$dir/librelane")"
expect_eq "BASH_ENV puts no PYTHONPATH back" "$(grep '^PYTHONPATH=' <<< "$out")" "PYTHONPATH=$cf/src:$sp/reference/python"
expect_eq "... nor NIX_PYTHONPATH" "$(grep '^NIX_PYTHONPATH=' <<< "$out")" "NIX_PYTHONPATH=<unset>"
expect_eq "... and is not handed on" "$(grep -E '^(BASH_ENV|ENV)=' <<< "$out")" "BASH_ENV=<unset>
ENV=<unset>"
out="$(env NIX_PYTHONPATH=/hostile 'BASH_FUNC_unset%%=() { :; }' "$dir/librelane")"
expect_eq "an exported unset does not stop the drop" "$(grep '^NIX_PYTHONPATH=' <<< "$out")" \
    "NIX_PYTHONPATH=<unset>"
out="$(env 'BASH_FUNC_echo%%=() { builtin printf "hijacked\\n"; }' "$dir/librelane")"
expect_eq "an exported function is not the wrapper's either" "$(grep -c hijacked <<< "$out")" 0
expect_eq "... which still runs" "$(grep '^PYTHONPATH=' <<< "$out")" "PYTHONPATH=$cf/src:$sp/reference/python"

# --- the roots, from the resolved trees
expect_eq "roots from the trees, one per launcher tree" "$(adk_nix_launcher_roots "$trees")" "$cf/src
$sp/reference/python"
expect 1 "a tree the shell did not resolve" adk_nix_launcher_roots "chiplet-flow|$cf|."
expect 1 "the spec tree not resolved" adk_nix_launcher_roots "$(grep -v '^chiplet-spec|' <<< "$trees")"
expect_out "... refused for it" "the CLI shell resolved no chiplet-spec tree"
expect 1 "the spec's rows resolved to two trees" \
    adk_nix_launcher_roots "$trees"$'\n'"chiplet-spec|$work/src/other-spec|description"
expect_out "... refused for it" "the CLI shell resolved chiplet-spec to more than one tree"
rm "$cf/src/librelane_plugin_chiplet/__init__.py"
expect 1 "a tree without the plugin" adk_nix_launcher_roots "$trees"
touch "$cf/src/librelane_plugin_chiplet/__init__.py"
rm "$sp/reference/python/chiplet_format_io/__init__.py"
expect 1 "a spec tree without the reader" adk_nix_launcher_roots "$trees"
expect_out "... refused for it" "chiplet-spec has no reference/python/chiplet_format_io/__init__.py"
touch "$sp/reference/python/chiplet_format_io/__init__.py"

# --- the entry
build_env() {
    ADK_NIX_LIBRELANE="$librelane" ADK_NIX_SG13G2_PDK="$pdk" ADK_NIX_TREES="${use_trees:-$trees}" \
        ADK_NIX_STATE="$work/state" ADK_NIX_MODE=dev "$@"
}
show_entry() {
    adk_nix_build_main 2>/dev/null
    echo "$PDK_ROOT|$PDK|$CHIPLET_FLOW_LIBRELANE_LAUNCHER|$CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256"
}
entry="$(build_env show_entry)"
IFS='|' read -r e_root e_pdk e_launcher e_sha <<< "$entry"
expect_eq "PDK_ROOT is the store tree" "$e_root" "$pdk"
expect_eq "PDK" "$e_pdk" ihp-sg13g2
expect_eq "launcher digest exported" "$e_sha" "$(sha256sum "$e_launcher" | cut -d' ' -f1)"
expect_eq "launcher under the state directory" "${e_launcher%/librelane-launcher-*/librelane}" "$work/state"
expect_eq "launcher names the two roots" "$(grep '^export PYTHONPATH=' "$e_launcher")" \
    "export PYTHONPATH='$cf/src:$sp/reference/python'"
expect_eq "the same trees, the same launcher" "$(build_env show_entry)" "$entry"
show_checkout() { adk_nix_build_main 2>/dev/null; echo "${LIBRELANE_CHECKOUT-<unset>}"; }
# A host value would sit under the binding below and show through the unset.
unset LIBRELANE_CHECKOUT
expect_eq "no LibreLane checkout handed on" "$(LIBRELANE_CHECKOUT="$work" build_env show_checkout)" "<unset>"
other_cf="$work/src2/chiplet-flow"
mkdir -p "$other_cf"
cp -r "$cf/src" "$other_cf/"
entry2="$(use_trees="${trees//$cf/$other_cf}" build_env show_entry)"
IFS='|' read -r _ _ e2_launcher e2_sha <<< "$entry2"
expect_eq "other trees: an entry" "$e2_sha" "$(sha256sum "$e2_launcher" 2>/dev/null | cut -d' ' -f1)"
expect 1 "... another launcher" test "$e2_launcher" = "$e_launcher"
expect_eq "... over the other root" "$(grep '^export PYTHONPATH=' "$e2_launcher" 2>/dev/null)" \
    "export PYTHONPATH='$other_cf/src:$sp/reference/python'"
expect_eq "... and the first one is untouched" "$(sha256sum "$e_launcher" | cut -d' ' -f1)" "$e_sha"
for var in ADK_NIX_LIBRELANE ADK_NIX_SG13G2_PDK ADK_NIX_TREES ADK_NIX_STATE ADK_NIX_MODE; do
    expect 1 "entry without $var" build_env env -u "$var" bash -c \
        "ADK_NIX_LIB_ONLY=1; . '$here/cli-env.sh'; ADK_NIX_LIB_ONLY=1; . '$here/build-env.sh'; adk_nix_build_main"
done
expect 1 "a PDK without the librelane tree" build_env env ADK_NIX_SG13G2_PDK="$bash_pkg" bash -c \
    "ADK_NIX_LIB_ONLY=1; . '$here/cli-env.sh'; ADK_NIX_LIB_ONLY=1; . '$here/build-env.sh'; adk_nix_build_main"
mkdir -p "$work/pdk-copy" && cp -r "$pdk/." "$work/pdk-copy/"
expect 1 "a PDK outside the store" build_env env ADK_NIX_SG13G2_PDK="$work/pdk-copy" bash -c \
    "ADK_NIX_LIB_ONLY=1; . '$here/cli-env.sh'; ADK_NIX_LIB_ONLY=1; . '$here/build-env.sh'; adk_nix_build_main"
expect 1 "evidence mode on a stand-in store" build_env env ADK_NIX_MODE=evidence ADK_NIX_VENV=/v \
    ADK_NIX_LIB=/l ADK_NIX_CONTEXT_OUT=/c bash -c \
    "ADK_NIX_LIB_ONLY=1; . '$here/cli-env.sh'; ADK_NIX_LIB_ONLY=1; . '$here/build-env.sh'; adk_nix_build_main"
expect_out "... refused for the store" "ADK_NIX_STORE_DIR must be /nix/store"

# A tree refused after another was resolved: the entry stops, with no launcher.
rm "$sp/reference/python/chiplet_format_io/__init__.py"
before="$(find "$work/state" -name librelane | sort)"
expect 1 "chiplet-flow resolved, chiplet-spec refused" build_env env bash -c \
    "ADK_NIX_LIB_ONLY=1; . '$here/cli-env.sh'; ADK_NIX_LIB_ONLY=1; . '$here/build-env.sh'; adk_nix_build_main"
expect_out "... for chiplet-spec" "chiplet-spec has no reference/python/chiplet_format_io/__init__.py"
expect_no_out "... says nothing is ready" "build shell ready"
expect_eq "... and writes no launcher" "$(find "$work/state" -name librelane | sort)" "$before"
touch "$sp/reference/python/chiplet_format_io/__init__.py"

# Evidence mode: the capture runs, from the lib directory, into the context
# directory; its refusal stops the entry. A stand-in capture records how it
# was called.
lib="$work/lib"
venv="$work/venv"
mkdir -p "$lib" "$venv/bin" "$work/ctx"
cat > "$venv/bin/python" <<'PY'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAPTURE_LOG"
echo "launcher=$CHIPLET_FLOW_LIBRELANE_LAUNCHER" >> "$CAPTURE_LOG"
exit "${CAPTURE_EXIT:-0}"
PY
chmod +x "$venv/bin/python"
evidence() {
    build_env env ADK_NIX_MODE=evidence ADK_NIX_STORE_DIR=/nix/store ADK_NIX_VENV="$venv" \
        ADK_NIX_LIB="$lib" ADK_NIX_CONTEXT_OUT="$work/ctx" CAPTURE_LOG="$work/capture.log" "$@" \
        bash -c "ADK_NIX_LIB_ONLY=1; . '$here/cli-env.sh'; ADK_NIX_LIB_ONLY=1; . '$here/build-env.sh'; \
                 adk_nix_store_path() { true; }; adk_nix_wrapper_bash() { echo '$bash_pkg/bin/bash'; }; \
                 adk_nix_build_main"
}
expect 0 "evidence entry runs the capture" evidence
expect_eq "... isolated, with the lib's script, into the context directory" "$(head -n4 "$work/capture.log")" \
    "-I
-S
$lib/capture_build_context.py
$work/ctx/build-context.json"
expect_eq "... after the launcher is exported" "$(grep -c '^launcher=/' "$work/capture.log")" 1
expect 1 "a refused capture stops the entry" evidence CAPTURE_EXIT=1
expect_out "... as the capture's refusal" "build context capture failed"
expect_no_out "... and says nothing is ready" "build shell ready"
for var in ADK_NIX_VENV ADK_NIX_LIB ADK_NIX_CONTEXT_OUT; do
    expect 1 "evidence entry without $var" evidence "$var="
    expect_out "... refused for it" "evidence: $var is not set"
done


# adk_nix_staged_roots: one root per launcher tree, under the staging given.
expect_eq "staged roots" "$(adk_nix_staged_roots /opt/s)" \
    "/opt/s/chiplet-flow/src
/opt/s/chiplet-spec/reference/python"

# adk_nix_die_build_env: the three it sets for a tree with LibreLane's files,
# and nothing for one without.
mkdir -p "$work/die-pdk/ihp-sg13g2/libs.tech/librelane"
expect_eq "die build env" "$(PDK_ROOT=/elsewhere PDK=x LIBRELANE_CHECKOUT=/c; \
    adk_nix_die_build_env "$work/die-pdk"; \
    echo "$PDK_ROOT|$PDK|${LIBRELANE_CHECKOUT-unset}|$(printenv PDK_ROOT)")" \
    "$work/die-pdk|ihp-sg13g2|unset|$work/die-pdk"
expect 1 "die build env for a tree without LibreLane files" adk_nix_die_build_env "$work/die-pdk/ihp-sg13g2"
expect_eq "... sets nothing" "$(PDK_ROOT=/elsewhere PDK=x LIBRELANE_CHECKOUT=/c; \
    adk_nix_die_build_env "$work/die-pdk/ihp-sg13g2"; echo "$PDK_ROOT|$PDK|$LIBRELANE_CHECKOUT")" \
    "/elsewhere|x|/c"

echo "build-env: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
