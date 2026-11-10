#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for nix-tail/check-tail-wrappers.sh, without Nix or network. The check
# runs against a stand-in adk-tools checkout that holds this checkout's own
# flake.nix, templates and image wrappers as committed files, with a `nix` and
# a `curl` on PATH that record any call, so a pass also shows the check called
# neither. Each refusal is reached by breaking exactly one thing, committed,
# against the setup that passes.
#
# Needs bash, git, python3 and coreutils. Writes only under one mktemp
# directory.
#
# Usage: nix-tail/test-check-tail-wrappers.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$(cd "$here/.." && pwd)"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-tail-wrappers-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
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

g() { git -C "$@"; }

mkdir -p "$work/bin"
for tool in nix curl; do
    printf '#!/bin/sh\necho "%s $*" >> "%s/called"\nexit 1\n' "$tool" "$work" > "$work/bin/$tool"
    chmod +x "$work/bin/$tool"
done
export PATH="$work/bin:$PATH"

names="chiplet-export adk-verify-carrier-drc adk-drc"
files="flake.nix nix/cli-env.sh nix-tail/tail-env.sh nix-tail/check-tail-wrappers.sh"
for name in $names; do
    files="$files bin/$name nix-tail/$name.in"
done
at="$work/at"

# setup: a committed stand-in checkout with this checkout's files as they are
# on disk.
setup() {
    local f
    rm -rf "$at"
    git init -q "$at"
    for f in $files; do
        mkdir -p "$at/$(dirname "$f")"
        cp "$src/$f" "$at/$f"
    done
    g "$at" add -A
    g "$at" commit -q -m base
}

# edit <file> <python expression over s>: rewrite one file and commit it. An
# edit that changes nothing, or a once(s, old, new) whose old is not in s
# exactly once, fails the suite.
edit() {
    if ! python3 - "$at/$1" "$2" <<'EOF'
import sys
path, expr = sys.argv[1], sys.argv[2]
with open(path) as handle:
    s = handle.read()


def once(s, old, new):
    if s.count(old) != 1:
        sys.exit(f"{old!r} is in {path} {s.count(old)} times, not once")
    return s.replace(old, new)


new = eval(expr)
if new == s:
    sys.exit(f"the edit changed nothing in {path}")
with open(path, "w") as handle:
    handle.write(new)
EOF
    then
        failed=$((failed + 1))
        echo "FAIL: the edit of $1 did not apply"
        return
    fi
    g "$at" commit -q -am "edit $1"
}

check() { bash "$at/nix-tail/check-tail-wrappers.sh" "$at"; }

# --- passes ----------------------------------------------------------------

setup
expect 0 "this checkout's wrappers, templates and flake pass" check
expect_out "the pass names the three commands" "tail wrappers: chiplet-export adk-verify-carrier-drc adk-drc, each in the image's shape"
expect 0 "the default checkout is the script's own" bash "$at/nix-tail/check-tail-wrappers.sh"
expect 1 "neither nix nor curl was called" test -e "$work/called"

setup
edit nix-tail/adk-drc.in 's.replace("exec ", "# a comment line\n\nexec ")'
expect 0 "comment and blank lines before the exec are allowed" check

setup
echo "exec /bin/false" > "$at/nix-tail/adk-drc.in"
expect 0 "an edit that is not committed is not what is checked" check

# --- refusals, one break each ----------------------------------------------

setup
g "$at" rm -q nix-tail/adk-drc.in && g "$at" commit -q -m "no template"
expect 1 "a missing template refuses" check
expect_out "the refusal names the file" "nix-tail/adk-drc.in is not in HEAD"

setup
g "$at" rm -q bin/chiplet-export && g "$at" commit -q -m "no image wrapper"
expect 1 "a missing image wrapper refuses" check
expect_out "the refusal names the image's file" "bin/chiplet-export is not in HEAD"

setup
edit nix-tail/chiplet-export.in 's.replace("#!@bash@ -p", "#!@bash@")'
expect 1 "a shebang without -p refuses" check
expect_out "the refusal names the first line" "chiplet-export.in: the first line is '#!@bash@'"

setup
edit nix-tail/adk-drc.in 's.replace(" \"$@\"", " --fast \"$@\"")'
expect 1 "an argument put before the caller's refuses" check
expect_out "the refusal shows the exec line" "adk-drc.in: the exec line is not"

setup
edit nix-tail/adk-drc.in 's.replace(" \"$@\"", " \"$1\"")'
expect 1 "passing only the first argument refuses" check

setup
edit nix-tail/adk-drc.in 's.replace(" \"$@\"", " \"$@\" --fast")'
expect 1 "an argument appended after the caller's refuses" check

setup
edit nix-tail/adk-drc.in 's.replace("exec ", "export KLAYOUT_PATH=/opt\nexec ")'
expect 1 "a line that is not a guard, the export or the exec refuses" check
expect_out "the refusal names the line" "a line that is not a guard.*KLAYOUT_PATH"

setup
edit nix-tail/adk-drc.in 's + "exec /bin/true \"$@\"\n"'
expect 1 "two exec lines refuse" check
expect_out "the refusal counts them" "adk-drc.in: 2 exec lines"

setup
edit nix-tail/adk-drc.in 's.replace("\"$ADK_NIX_VENV/bin/python\"", "/usr/bin/python3")'
expect 1 "a literal host interpreter refuses" check

setup
edit nix-tail/adk-drc.in 's.replace("\"$ADK_NIX_VENV/bin/python\"", "\"$HOME/bin/python\"")'
expect 1 "a variable that is not one of the three roots refuses" check
expect_out "the refusal names it" "names \\\$HOME, which is not"

setup
edit nix-tail/adk-drc.in 's.replace("$ADK_ROOT/klayout/drc/run_drc.py", "$ADK_ROOT/../adk/klayout/drc/run_drc.py")'
expect 1 "a path that climbs out of its root refuses" check

setup
edit nix-tail/adk-drc.in 's.replace("$ADK_ROOT/klayout/drc/run_drc.py", "$ADK_ROOT/klayout/drc/run_lvs.py")'
expect 1 "another script than the image's refuses" check
expect_out "the refusal names both" "adk-drc.in: runs .*run_lvs.py.*, not \\\$ADK_ROOT/klayout/drc/run_drc.py"

setup
edit nix-tail/chiplet-export.in 's.replace("$ADK_NIX_PLUGIN_ROOT/plugins", "$ADK_ROOT/plugins")'
expect 1 "the image's script under another root refuses" check

setup
edit bin/adk-drc 's.replace("run_drc.py\" \"$@\"", "run_drc.py\" --fast \"$@\"")'
expect 1 "an image wrapper that no longer passes \"\$@\" last refuses" check
expect_out "the refusal names the image's file" "bin/adk-drc: does not end by running"

setup
edit nix-tail/adk-verify-carrier-drc.in 's.replace("@carrierDrc@", "@exportPython@/carrier")'
expect 1 "the carrier wrapper not running the store copy refuses" check
expect_out "the refusal says what it runs instead" "does not run the store copy of bin/adk-verify-carrier-drc"

for c in '\r' '\x0c' '\x85' '\xa0'; do
    setup
    edit nix-tail/adk-drc.in "s.replace('}\"\\nexec', '}\"$c# ; export KLAYOUT_PATH=/opt\\nexec')"
    expect 1 "a $c that hides a command from the check refuses" check
    expect_out "the refusal names the character" "adk-drc.in: holds"
done

setup
edit nix-tail/adk-drc.in 's.replace("\n", "\r\n")'
expect 1 "a template with CRLF line ends refuses" check

setup
edit nix-tail/adk-drc.in 's.replace(":?enter through devShells.tail", ":-/opt/adk-tools")'
expect 1 "a guard with a fallback path refuses" check

setup
edit nix-tail/chiplet-export.in 's.replace("\x27@exportPython@/bin/python\x27", "\"$ADK_NIX_VENV/bin/python\"")'
expect 1 "chiplet-export under the venv's interpreter refuses" check
expect_out "the refusal names both" "chiplet-export.in: runs under \\\$ADK_NIX_VENV/bin/python, not @exportPython@/bin/python"

setup
edit nix-tail/adk-drc.in 's.replace("exec \"$ADK_NIX_VENV/bin/python\"", "exec \x27@exportPython@/bin/python\x27")'
expect 1 "adk-drc under the export interpreter refuses" check
expect_out "the refusal names both" "adk-drc.in: runs under @exportPython@/bin/python, not \\\$ADK_NIX_VENV/bin/python"

setup
edit nix-tail/adk-drc.in 's.replace(": \"${ADK_ROOT:?enter through devShells.tail}\"\n", "")'
expect 1 "a guard dropped refuses" check
expect_out "the refusal lists the guards" "adk-drc.in: guards \\[.ADK_NIX_VENV.\\], not"

setup
edit nix-tail/chiplet-export.in 's.replace(": \"${PDK_ROOT:?enter through devShells.tail}\"\n", "")'
expect 1 "chiplet-export without its PDK_ROOT guard refuses" check

setup
edit nix-tail/chiplet-export.in 's.replace(": \"${GDS_TO_KICAD_ROOT:?enter through devShells.tail}\"\n", "")'
expect 1 "chiplet-export without its GDS_TO_KICAD_ROOT guard refuses" check
expect_out "the refusal lists the guards" "chiplet-export.in: guards .*, not .*GDS_TO_KICAD_ROOT"

setup
edit nix-tail/adk-drc.in 's.replace("exec ", ": \"${HOME:?enter through devShells.tail}\"\nexec ")'
expect 1 "a guard the command does not need refuses" check
expect_out "the refusal lists the guards" "adk-drc.in: guards .*HOME"

setup
edit nix-tail/adk-verify-carrier-drc.in 's.replace("export ADK_PYTHON=\"$ADK_NIX_VENV/bin/python\"\n", "")'
expect 1 "the carrier wrapper without the ADK_PYTHON export refuses" check
expect_out "the refusal says so" "adk-verify-carrier-drc.in: does not export ADK_PYTHON"

setup
edit nix-tail/adk-drc.in 's.replace("exec ", "export ADK_PYTHON=\"$ADK_NIX_VENV/bin/python\"\nexec ")'
expect 1 "an ADK_PYTHON export where the image's script starts no second Python refuses" check
expect_out "the refusal says so" "adk-drc.in: exports ADK_PYTHON"

setup
edit nix-tail/adk-verify-carrier-drc.in 's.replace("$ADK_NIX_VENV/bin/python\"\nexec", "/usr/bin/python3\"\nexec")'
expect 1 "an ADK_PYTHON export naming another interpreter refuses" check

setup
edit nix-tail/adk-verify-carrier-drc.in '(lambda g, e: s.replace(g + e, e + g))(": \"${ADK_NIX_VENV:?enter through devShells.tail}\"\n", "export ADK_PYTHON=\"$ADK_NIX_VENV/bin/python\"\n")'
expect 1 "the export ahead of the guard it needs refuses" check
expect_out "the refusal says in which order" "in that order"

setup
edit flake.nix 's.replace("--subst-var-by carrierDrc ${./bin/adk-verify-carrier-drc}", "--subst-var-by carrierDrc ${./bin/adk-drc}")'
expect 1 "a flake that fills @carrierDrc@ with another file refuses" check
expect_out "the refusal names it" "@carrierDrc@ is \\\$\\{\\./bin/adk-drc\\}"

setup
edit flake.nix 's.replace("--subst-var-by bash ${pkgs.bash}/bin/bash", "--subst-var-by bash /bin/bash")'
expect 1 "a placeholder filled with a literal path refuses" check
expect_out "the refusal says so" "@bash@ is /bin/bash, not \\\$\\{pkgs\\.bash\\}/bin/bash"

setup
edit flake.nix 's.replace("--subst-var-by exportPython ${tail-export-python} \\\n", "")'
expect 1 "a placeholder the flake no longer fills refuses" check
expect_out "the refusal names it" "the templates use @exportPython@ and nothing fills it"

setup
edit flake.nix 's.replace("--subst-var-by bash ${pkgs.bash}/bin/bash", "--subst-var-by bash ${pkgs.bash}/bin/bash --subst-var-by extra ${pkgs.bash}")'
expect 1 "a placeholder no template uses refuses" check
expect_out "the refusal names it" "fills @extra@, which no template uses"

setup
edit flake.nix 's.replace("\"adk-drc\"\n", "")'
expect 1 "a flake that builds two of the three wrappers refuses" check
expect_out "the refusal lists what it builds" "the wrappers built are"

# The flake is read in its one `tail-wrappers = ...` binding only, as it
# stands: a comment, or a copy in another binding the check reads, cannot
# stand in for what the build runs.
carrier='--subst-var-by carrierDrc ${./bin/adk-verify-carrier-drc}'
setup
edit flake.nix "s.replace('$carrier\n', '--subst-var-by carrierDrc \${./nix-tail/tail_eval.py}\n                # was: $carrier\n')"
expect 1 "another file for @carrierDrc@ behind a shell comment with the right one refuses" check
expect_out "the refusal names the comment" "the tail-wrappers binding holds a comment"

setup
edit flake.nix "s.replace('$carrier\n', '--subst-var-by carrierDrc \${./nix-tail/tail_eval.py}\n').replace('          # LibreLane as the die builds', '          # $carrier\n          # LibreLane as the die builds')"
expect 1 "another file for @carrierDrc@ with the right one in a Nix comment outside the binding refuses" check
expect_out "the refusal names the file the build uses" "@carrierDrc@ is \\\$\\{\\./nix-tail/tail_eval\\.py\\}"

setup
edit flake.nix 's.replace("substitute ${./nix-tail + \"/${name}.in\"}", "# substitute ${./nix-tail + \"/${name}.in\"}\n                substitute ${./nix-tail + \"/${name}.alt\"}")'
expect 1 "other templates behind a commented copy of the substitute call refuse" check

setup
edit flake.nix 's.replace("substitute ${./nix-tail + \"/${name}.in\"}", "substitute ${./nix-tail + \"/${name}.alt\"}")'
expect 1 "other templates refuse" check
expect_out "the refusal shows the call" "the substitute call is not"

setup
edit flake.nix "s.replace('$carrier', '--subst-var-by carrierDrc \${./nix-tail/tail_eval.py} \\\\\n                  $carrier')"
expect 1 "@carrierDrc@ filled twice refuses" check
expect_out "the refusal says so" "fills @carrierDrc@ twice"

setup
edit flake.nix 's.replace("--subst-var-by exportPython ${tail-export-python}", "--subst-var-by exportPython ${pkgs.python3}")'
expect 1 "@exportPython@ filled with another Nix value refuses" check
expect_out "the refusal names both" "@exportPython@ is \\\$\\{pkgs\\.python3\\}, not \\\$\\{tail-export-python\\}"

setup
edit flake.nix 's.replace("--subst-var-by bash ${pkgs.bash}/bin/bash", "--subst-var-by bash ${pkgs.bashInteractive}/bin/bash")'
expect 1 "@bash@ filled with another Nix value refuses" check

setup
edit flake.nix "s.replace('$carrier\n', '$carrier # the store copy\n')"
expect 1 "a trailing comment in the binding refuses" check

setup
edit flake.nix 's.replace("            chmod 0555 \"$out\"/bin/*\n", "            sed -i s/-p// \"$out\"/bin/*\n            chmod 0555 \"$out\"/bin/*\n")'
expect 1 "a command that edits the wrappers after the substitution refuses" check
expect_out "the refusal names the line" "the tail-wrappers binding's line 20 is 'sed -i s/-p// \"\\\$out\"/bin/\*'"

setup
edit flake.nix 's.replace("            chmod 0555 \"$out\"/bin/*\n", "            /* keep */ chmod 0555 \"$out\"/bin/*\n")'
expect 1 "a block comment in the binding refuses" check

setup
edit flake.nix 's.replace("tail-wrappers = pkgs.runCommandLocal", "tail-wrappers-unused = pkgs.runCommandLocal").replace("          # LibreLane as the die builds", "          tail-wrappers = pkgs.writeShellScriptBin \"chiplet-export\" \"\";\n\n          # LibreLane as the die builds")'
expect 1 "tail-wrappers bound to another derivation refuses" check

setup
edit flake.nix 's.replace("          # LibreLane as the die builds", "          tail-wrappers = pkgs.hello;\n\n          # LibreLane as the die builds")'
expect 1 "tail-wrappers bound twice refuses" check
expect_out "the refusal says so" "2 lines read like a tail-wrappers binding in the form \`tail-wrappers =\`; this check reads exactly one"

setup
edit flake.nix "s.replace('$carrier\n', '$carrier\x0b\n')"
expect 1 "a vertical tab after a value, which Python strips and bash keeps, refuses" check
expect_out "the refusal names it" "the tail-wrappers binding holds \\['\\\\x0b'\\]; it takes printable ASCII only"

setup
edit flake.nix 's.replace("--subst-var-by exportPython ${tail-export-python} \\\n", "--subst-var-by exportPython ${tail-export-python} \\\n\n")'
expect 1 "a blank line that ends the substitute call early refuses" check
expect_out "the refusal shows the call it ends" "the substitute call is not .*exportPython \\\$\\{tail-export-python\\} '$"

setup
edit flake.nix 's.replace("          # LibreLane as the die builds", "          # tail-wrappers = pkgs.hello;\n          # LibreLane as the die builds")'
expect 0 "a comment line that reads like a binding is not a second binding" check

# A line that starts with # but can end a string or a comment before a
# binding on it is counted: Nix may read that binding.
setup
edit flake.nix 's.replace("          # LibreLane as the die builds", "          note = \x27\x27\n          #\x27\x27; tail-wrappers = pkgs.hello;\n          # LibreLane as the die builds")'
expect 1 "a binding after a # line that closes a '' string refuses" check
expect_out "the refusal counts it" "2 lines read like a tail-wrappers binding"

setup
edit flake.nix 's.replace("          # LibreLane as the die builds", "          note = \"\n          #\"; tail-wrappers = pkgs.hello;\n          # LibreLane as the die builds")'
expect 1 "a binding after a # line that closes a \" string refuses" check

setup
edit flake.nix 's.replace("          # LibreLane as the die builds", "          /*\n          # */ tail-wrappers = pkgs.hello;\n          # LibreLane as the die builds")'
expect 1 "a binding after a # line that closes a block comment refuses" check

setup
edit flake.nix 's.replace("          # LibreLane as the die builds", "          # wrappers\rtail-wrappers = pkgs.hello;\n          # LibreLane as the die builds")'
expect 1 "a binding after a carriage return that ends a # comment refuses" check

setup
edit flake.nix 's.replace("          # LibreLane as the die builds", "          x = 1; # was: tail-wrappers = pkgs.hello;\n          # LibreLane as the die builds")'
expect 1 "a trailing comment that reads like a binding refuses, failing closed" check
expect_out "the refusal says only what it read" "2 lines read like a tail-wrappers binding"

# Only leading spaces and tabs are indentation.
setup
edit flake.nix 's.replace("--subst-var-by exportPython ${tail-export-python} \\\n", "--subst-var-by exportPython ${tail-export-python} \\ \n")'
expect 1 "a space after a closing backslash, where bash ends the line, refuses" check
expect_out "the refusal shows the call it ends" "the substitute call is not .*exportPython \\\$\\{tail-export-python\\} \\\\\\\\'$"

setup
edit flake.nix 's.replace("mkdir -p \"$out/bin\"\n", "mkdir -p \"$out/bin\" \n", 1)'
expect 1 "a space after a line of the binding refuses" check
expect_out "the refusal names the line" "the tail-wrappers binding's line 2 is 'mkdir -p \"\\\$out/bin\" ', not"

# Other forms of binding are outside what this check reads, and it says so.
# Each edit here is a flake Nix parses: packages.tail-wrappers, which the tail
# shell takes, is no longer inherited from the let but bound in the output set
# to an override that strips -p after the substitutions. This check passes it.
# check-tail-eval.sh reads the derivation Nix evaluates instead, and
# test_tail_eval.py's RealShows.test_an_override_that_strips_p_refuses refuses
# that override's build command on what Nix 2.30.1 printed for this flake.
# Both read the command the override appends from tests/nix-tail/strip-p-command.
strip_p="$(cat "$src/tests/nix-tail/strip-p-command")"
override="tail-wrappers.overrideAttrs (o: { buildCommand = o.buildCommand + \"$strip_p\"; });"
rebind() {  # rebind <how packages.tail-wrappers is bound in the output set>
    edit flake.nix "once(once(s, '            tail-export-python\n            tail-wrappers\n            opensta\n', '            tail-export-python\n            opensta\n'), '          librelane = librelane-patched;\n', '          librelane = librelane-patched;\n' + $1 + '\n')"
}
setup
rebind "'          \"tail-wrappers\" = ' + '$override'"
expect 0 "packages.tail-wrappers quoted and overridden in the output set is not read here" check

setup
rebind "'          tail-wrappers\n            = ' + '$override'"
expect 0 "packages.tail-wrappers split over two lines and overridden is not read here" check

setup
edit nix/cli-env.sh 's.replace("ADK_ROOT|tools/adk", "ADK_ROOT|tools/adk-fork")'
expect 1 "ADK_ROOT rooted elsewhere in evidence mode refuses" check

setup
edit nix-tail/tail-env.sh 's.replace("ADK_NIX_TAIL_PLUGIN_PATH=tools/chiplet_kicad_plugin", "ADK_NIX_TAIL_PLUGIN_PATH=tools/plugin")'
expect 1 "the plugin rooted elsewhere in evidence mode refuses" check

echo "check-tail-wrappers tests: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
