#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for the checks in nix/cli-env.sh, without Nix: the functions are
# sourced on their own (ADK_NIX_LIB_ONLY=1) and run against throwaway git
# repositories. Every refusal the evidence entry relies on is reached here by
# breaking exactly one thing, so none of them is dead code that happens to
# pass.
#
# Needs bash, git, coreutils and a python3 (the venv's lock and the tool table's
# reader run through an interpreter). Writes only under one mktemp directory.
#
# Usage: nix/test-cli-env.sh

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/cli-env.sh"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-nix-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
# $work as the hook resolves the paths it builds on it.
rwork="$(realpath "$work")"
test_python="$(command -v python3)" || { echo "cli-env: no python3 on PATH" >&2; exit 1; }
host_git="$(command -v git)" || { echo "cli-env: no git on PATH" >&2; exit 1; }

# Nothing from the caller's git configuration decides a result here.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid

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

# A repository with one commit and a pyproject; prints its commit.
mkrepo() {
    git init -q "$1"
    printf '[project]\nname = "%s"\n' "$(basename "$1")" > "$1/pyproject.toml"
    printf '*.egg-info/\n__pycache__/\n' > "$1/.gitignore"
    git -C "$1" add -A
    git -C "$1" commit -q -m init
    git -C "$1" rev-parse HEAD
}

# ---------------------------------------------------------------- helpers

expect_eq "src var for chiplet-flow" "$(adk_nix_src_var chiplet-flow)" ADK_NIX_SRC_CHIPLET_FLOW
expect_eq "src var for chiplet-spec" "$(adk_nix_src_var chiplet-spec)" ADK_NIX_SRC_CHIPLET_SPEC
expect_eq "src var for assembly-lvs" "$(adk_nix_src_var assembly-lvs)" ADK_NIX_SRC_ASSEMBLY_LVS

one="$work/one"
one_sha="$(mkrepo "$one")"
expect_eq "clean checkout state" "$(adk_nix_checkout_state "$one")" "$one_sha clean"
expect 1 "a plain directory is not a checkout" adk_nix_checkout_state "$work"

expect 0 "pinned: clean, at the pin" adk_nix_require_pinned one "$one" "$one_sha"
expect 1 "pinned: another commit" adk_nix_require_pinned one "$one" 0000000000000000000000000000000000000000
expect 1 "pinned: empty pin" adk_nix_require_pinned one "$one" ""
expect 1 "pinned: not a checkout" adk_nix_require_pinned one "$work" "$one_sha"
touch "$one/untracked.py"
expect_eq "untracked file is dirty" "$(adk_nix_checkout_state "$one")" "$one_sha dirty"
expect 1 "pinned: dirty" adk_nix_require_pinned one "$one" "$one_sha"
rm "$one/untracked.py"
expect 1 "pinned: unknown kind" adk_nix_require_pinned one "$one" "$one_sha" nonsense

# Things that hide a change from a plain `git status`. Each refuses.
echo "# edit" >> "$one/pyproject.toml"
git -C "$one" update-index --assume-unchanged pyproject.toml
expect_eq "assume-unchanged hides the edit from plain status" "$(git -C "$one" status --porcelain)" ""
expect 1 "pinned: assume-unchanged entry" adk_nix_require_pinned one "$one" "$one_sha"
git -C "$one" update-index --no-assume-unchanged pyproject.toml
git -C "$one" update-index --skip-worktree pyproject.toml
expect 1 "pinned: skip-worktree entry" adk_nix_require_pinned one "$one" "$one_sha"
git -C "$one" update-index --no-skip-worktree pyproject.toml
git -C "$one" checkout -q -- pyproject.toml
expect 0 "pinned: flags cleared, clean again" adk_nix_require_pinned one "$one" "$one_sha"

touch "$one/stray.py"
git -C "$one" config status.showUntrackedFiles no
expect 1 "pinned: untracked file behind status.showUntrackedFiles=no" adk_nix_require_pinned one "$one" "$one_sha"
git -C "$one" config --unset status.showUntrackedFiles
printf '*.py\n' > "$work/excludes"
git -C "$one" config core.excludesFile "$work/excludes"
expect 1 "pinned: untracked file behind core.excludesFile" adk_nix_require_pinned one "$one" "$one_sha"
git -C "$one" config --unset core.excludesFile
# A rule in info/exclude is fine while it hides nothing; a file it hides is
# an ignored file, and refuses.
printf '*.py\n' >> "$one/.git/info/exclude"
expect_eq "info/exclude hides the file from plain status" "$(git -C "$one" status --porcelain)" ""
expect 1 "pinned: a file hidden by info/exclude" adk_nix_require_pinned one "$one" "$one_sha"
rm "$one/stray.py"
expect 0 "pinned: a rule in info/exclude that hides nothing" adk_nix_require_pinned one "$one" "$one_sha"
sed -i '$d' "$one/.git/info/exclude"

# The caller's git environment is not used: GIT_DIR naming another repository
# would otherwise make -C look at that one.
with_env() { export "$1"; shift; "$@"; }
expect 0 "pinned: the caller's GIT_DIR is not used" \
    with_env GIT_DIR="$work/nowhere/.git" adk_nix_require_pinned one "$one" "$one_sha"

# Ignored files, by kind. A root allows none; a tree installed from <dir>
# allows the .egg-info an editable install may leave in <dir> or <dir>/src,
# and nothing else. Git lists no empty directory, so each holds a file.
lay="$work/lay"
mkrepo "$lay" >/dev/null
mkdir -p "$lay/src/pkg" "$lay/sub/src/pkg"
touch "$lay/src/pkg/__init__.py" "$lay/sub/src/pkg/__init__.py"
git -C "$lay" add -A
git -C "$lay" commit -q -m layout
lay_sha="$(git -C "$lay" rev-parse HEAD)"
expect 0 "kinds: clean root" adk_nix_require_pinned lay "$lay" "$lay_sha" root
egg() { mkdir -p "$lay/$1" && touch "$lay/$1/PKG-INFO"; }
egg pkg.egg-info
expect 0 "kinds: egg-info at the install dir" adk_nix_require_pinned lay "$lay" "$lay_sha" source:.
expect 1 "kinds: egg-info in a root" adk_nix_require_pinned lay "$lay" "$lay_sha" root
expect 1 "kinds: egg-info outside the install dir" adk_nix_require_pinned lay "$lay" "$lay_sha" source:sub
rm -r "$lay/pkg.egg-info"
egg src/pkg.egg-info
expect 0 "kinds: egg-info in src/" adk_nix_require_pinned lay "$lay" "$lay_sha" source:.
rm -r "$lay/src/pkg.egg-info"
egg sub/src/pkg.egg-info
expect 0 "kinds: egg-info in <dir>/src/" adk_nix_require_pinned lay "$lay" "$lay_sha" source:sub
expect 1 "kinds: egg-info deeper than the install dir allows" \
    adk_nix_require_pinned lay "$lay" "$lay_sha" source:.
expect 0 "kinds: egg-info in the second of two install dirs" \
    adk_nix_require_pinned lay "$lay" "$lay_sha" "source:. sub"
rm -r "$lay/sub/src/pkg.egg-info"
mkdir "$lay/src/pkg/__pycache__"
touch "$lay/src/pkg/__pycache__/__init__.cpython-313.pyc"
expect_eq "an ignored __pycache__ is invisible to plain status" "$(git -C "$lay" status --porcelain)" ""
expect 1 "kinds: ignored bytecode in a source tree" adk_nix_require_pinned lay "$lay" "$lay_sha" source:.
expect 1 "kinds: ignored bytecode in a root" adk_nix_require_pinned lay "$lay" "$lay_sha" root
rm -r "$lay/src/pkg/__pycache__"
mkdir "$lay/hid"
printf '*\n' > "$lay/hid/.gitignore"
touch "$lay/hid/evil.py"
expect_eq "a self-ignoring .gitignore hides itself and its directory" "$(git -C "$lay" status --porcelain)" ""
expect 1 "kinds: a self-ignoring untracked .gitignore" adk_nix_require_pinned lay "$lay" "$lay_sha" source:.
rm -r "$lay/hid"
expect 0 "kinds: restored" adk_nix_require_pinned lay "$lay" "$lay_sha" source:.

# core.worktree points git at another directory: status there is clean while
# this one is edited.
cp -r "$lay" "$work/decoy"
rm -rf "$work/decoy/.git"
git -C "$lay" config core.worktree "$work/decoy"
echo "# edit" >> "$lay/pyproject.toml"
expect_eq "core.worktree hides the edit from plain status" "$(git -C "$lay" status --porcelain)" ""
expect 1 "pinned: core.worktree elsewhere" adk_nix_require_pinned lay "$lay" "$lay_sha" root
git -C "$lay" config --unset core.worktree
git -C "$lay" checkout -q -- pyproject.toml

# A clean filter can make an edited file hash as committed. Refused whether it
# is set directly or through an include.
git -C "$lay" config filter.hide.clean cat
expect 1 "pinned: a content filter in the repository config" adk_nix_require_pinned lay "$lay" "$lay_sha" root
git -C "$lay" config --remove-section filter.hide
printf '[filter "hide"]\n\tclean = cat\n' > "$work/included"
git -C "$lay" config include.path "$work/included"
expect 1 "pinned: a content filter through an include" adk_nix_require_pinned lay "$lay" "$lay_sha" root
git -C "$lay" config --unset include.path
expect 0 "pinned: restored" adk_nix_require_pinned lay "$lay" "$lay_sha" root

pin="$work/pin"
cat > "$pin" <<'EOF'
echo "noise from the pin file"
STAGE_X_REF=abc123
ADK_NIX_MODE=hijacked
EOF
ADK_NIX_MODE=dev
expect_eq "pin value" "$(adk_nix_pin "$pin" STAGE_X_REF)" abc123
expect_eq "unset pin reads empty" "$(adk_nix_pin "$pin" STAGE_Y_REF)" ""
adk_nix_pin "$pin" STAGE_X_REF >/dev/null
expect_eq "the pin file cannot change this shell" "$ADK_NIX_MODE" dev
expect 0 "a pin file that sources cleanly reads status 0" adk_nix_pin "$pin" STAGE_X_REF
printf 'return 1\n' >> "$pin"
expect 1 "a pin file that stops with a failure reads its status" adk_nix_pin "$pin" STAGE_X_REF
expect_eq "and still gives the value it reached" "$(adk_nix_pin "$pin" STAGE_X_REF)" abc123
# A failure before the last line, which the status of `.` alone does not show.
printf 'false\nSTAGE_X_REF=abc123\n' > "$pin"
expect 1 "a failing command mid-file reads non-zero" adk_nix_pin "$pin" STAGE_X_REF
printf 'A=$(exit 3)\nSTAGE_X_REF=abc123\n' > "$pin"
expect 1 "a failed substitution mid-file reads non-zero" adk_nix_pin "$pin" STAGE_X_REF
printf 'A="$UNSET_IN_THIS_PIN"\nSTAGE_X_REF=abc123\n' > "$pin"
expect 1 "an unset reference mid-file reads non-zero" adk_nix_pin "$pin" STAGE_X_REF
printf 'adk_nix_pin_var=STAGE_Y_REF\nSTAGE_X_REF=abc123\n' > "$pin"
expect 1 "the pin file cannot change which variable is read" adk_nix_pin "$pin" STAGE_X_REF
# And the same checked the way check-openroad-pin.sh checks it.
printf 'false\nSTAGE_X_REF=abc123\n' > "$pin"
value="$(adk_nix_pin "$pin" STAGE_X_REF)" && rc=0 || rc=$?
expect_eq "a mid-file failure is seen through value=\"\$(...)\" ||" "$rc" 1

# ---------------------------------------------------------------- trees

src="$work/src"
mkdir -p "$src"
for name in chiplet-flow packaging-flow interposer-pnr chiplet-spec assembly-lvs chiplet-cosim; do
    mkrepo "$src/$name" >/dev/null
done
# chiplet-spec's two install dirs, each holding its package as the real tree does.
mkdir -p "$src/chiplet-spec/reference/python/chiplet_format_io" \
         "$src/chiplet-spec/description/src/chiplet_system_description"
touch "$src/chiplet-spec/reference/python/chiplet_format_io/__init__.py" \
      "$src/chiplet-spec/description/src/chiplet_system_description/__init__.py"
git -C "$src/chiplet-spec" add -A
git -C "$src/chiplet-spec" commit -q -m layout

(
    unset ADK_NIX_SOURCES ADK_NIX_SRC_CHIPLET_FLOW
    ADK_NIX_MODE=dev
    expect 1 "dev: no source named refuses" adk_nix_trees
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "dev: that refusal names the variable" \
        grep -q "chiplet-flow: set ADK_NIX_SRC_CHIPLET_FLOW or ADK_NIX_SOURCES" "$work/refusal.out"
    ADK_NIX_SOURCES="$src"
    # One line per row: chiplet-spec's two rows are the one tree.
    expect_eq "dev: ADK_NIX_SOURCES" \
        "$(adk_nix_trees | awk -F'|' '$1 == "chiplet-spec" { print $2 "|" $3 }')" \
        "$src/chiplet-spec|reference/python
$src/chiplet-spec|description"
    ADK_NIX_SRC_CHIPLET_FLOW="$work/elsewhere"
    expect_eq "dev: per-tree variable wins" \
        "$(adk_nix_trees | awk -F'|' '$1 == "chiplet-flow" { print $2 }')" "$work/elsewhere"
    expect_eq "dev: assembly-lvs is a tree" \
        "$(adk_nix_trees | awk -F'|' '$1 == "assembly-lvs" { print $2 "|" $3 }')" "$src/assembly-lvs|."
    # A sources directory from before assembly-lvs was a tree: the refusal
    # names both ways to give it.
    mv "$src/assembly-lvs" "$work/lvs-away"
    expect 1 "dev: ADK_NIX_SOURCES without assembly-lvs refuses" adk_nix_trees
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "dev: that refusal names both ways to give it" \
        grep -q "assembly-lvs: $src/assembly-lvs does not exist; put assembly-lvs in ADK_NIX_SOURCES or set ADK_NIX_SRC_ASSEMBLY_LVS" \
        "$work/refusal.out"
    ADK_NIX_SRC_ASSEMBLY_LVS="$work/lvs-away"
    expect_eq "dev: the per-tree variable alone gives it" \
        "$(adk_nix_trees | awk -F'|' '$1 == "assembly-lvs" { print $2 }')" "$work/lvs-away"
    unset ADK_NIX_SRC_ASSEMBLY_LVS
    mv "$work/lvs-away" "$src/assembly-lvs"
    mv "$src/chiplet-cosim" "$work/cosim-away"
    expect 1 "dev: ADK_NIX_SOURCES without chiplet-cosim refuses" adk_nix_trees
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "dev: that refusal names both ways to give chiplet-cosim" \
        grep -q "chiplet-cosim: $src/chiplet-cosim does not exist; put chiplet-cosim in ADK_NIX_SOURCES or set ADK_NIX_SRC_CHIPLET_COSIM" \
        "$work/refusal.out"
    mv "$work/cosim-away" "$src/chiplet-cosim"
    expect_eq "dev: chiplet-cosim is a tree" \
        "$(adk_nix_trees | awk -F'|' '$1 == "chiplet-cosim" { print $2 "|" $3 }')" "$src/chiplet-cosim|."
    ADK_NIX_MODE=evidence ADK_NIX_ADK_TOOLS="$work/at"
    expect_eq "evidence: always staging/, whatever dev names" \
        "$(adk_nix_trees | awk -F'|' '$1 == "chiplet-flow" { print $2 }')" "$work/at/staging/chiplet-flow"
    echo "$passed $failed" > "$work/counts"
)
read -r passed failed < "$work/counts"

# One line per tree, a tree's install directories together; the rows of one
# tree must name one ref variable.
expect_eq "table: chiplet-spec is one tree with two install dirs" \
    "$(adk_nix_editable_trees | grep '^chiplet-spec|')" \
    "chiplet-spec|STAGE_CHIPLET_SPEC_REF|reference/python description"
expect_eq "table: one line per tree" "$(adk_nix_editable_trees | wc -l)" \
    "$(awk -F'|' 'NF { print $1 }' <<< "$ADK_NIX_EDITABLE" | sort -u | wc -l)"
expect 1 "table: rows of one tree naming two refs refuse" \
    with_env ADK_NIX_EDITABLE="$ADK_NIX_EDITABLE"$'\n'"chiplet-spec|STAGE_CHIPLET_FLOW_REF|other" \
    adk_nix_editable_trees
cp "$work/last.out" "$work/refusal.out"
expect 0 "table: that refusal names both" \
    grep -qF "chiplet-spec: its rows name both STAGE_CHIPLET_SPEC_REF and STAGE_CHIPLET_FLOW_REF" \
    "$work/refusal.out"

# ---------------------------------------------------------------- evidence

# A small adk-tools: staging.pin naming the six trees, staging/ ignored and
# holding clones at those commits, three submodule gitlinks with checkouts.
at="$work/at"
git init -q "$at"
mkdir -p "$at/staging"
{
    for name in chiplet-flow packaging-flow interposer-pnr chiplet-spec assembly-lvs chiplet-cosim; do
        var="STAGE_${name//-/_}_REF"
        echo "${var^^}=$(git -C "$src/$name" rev-parse HEAD)"
        git clone -q "$src/$name" "$at/staging/$name"
    done
} > "$at/staging.pin"
printf 'staging/\n' > "$at/.gitignore"
: > "$at/.gitmodules"
for path in tools/adk tools/OpenIntM4TM2 tools/IHP-Interconnect-IntM4TM2; do
    sha="$(mkrepo "$at/$path")"
    git -C "$at" update-index --add --cacheinfo "160000,$sha,$path"
    printf '[submodule "%s"]\n\tpath = %s\n\turl = ./%s\n' "$path" "$path" "$path" >> "$at/.gitmodules"
done
git -C "$at" add staging.pin .gitignore .gitmodules
git -C "$at" commit -q -m "adk-tools"
at_sha="$(git -C "$at" rev-parse HEAD)"

expect_eq "gitlink of tools/adk" "$(adk_nix_gitlink "$at" tools/adk)" "$(git -C "$at/tools/adk" rev-parse HEAD)"
expect_eq "no gitlink for a file" "$(adk_nix_gitlink "$at" staging.pin)" ""

ADK_NIX_MODE=evidence
ADK_NIX_ADK_TOOLS="$at"
ADK_NIX_FLAKE_REV="$at_sha"
expect 0 "evidence: everything at its pin" adk_nix_check_evidence

# One thing broken at a time, and put back.
ADK_NIX_FLAKE_REV="$at_sha-dirty"
expect 1 "evidence: flake evaluated from a dirty tree" adk_nix_check_evidence
ADK_NIX_FLAKE_REV=unknown
expect 1 "evidence: flake evaluated from a non-git tree" adk_nix_check_evidence
ADK_NIX_FLAKE_REV="$one_sha"
expect 1 "evidence: flake rev is not this checkout" adk_nix_check_evidence
ADK_NIX_FLAKE_REV="$at_sha"

touch "$at/stray"
expect 1 "evidence: adk-tools checkout dirty" adk_nix_check_evidence
rm "$at/stray"

flow="$at/staging/chiplet-flow"
echo "# edit" >> "$flow/pyproject.toml"
expect 1 "evidence: staged tree dirty" adk_nix_check_evidence
git -C "$flow" commit -q -am "moved"
expect 1 "evidence: staged tree at another commit" adk_nix_check_evidence
git -C "$flow" reset -q --hard HEAD~1
mkdir "$flow/chiplet_die_flow.egg-info"
touch "$flow/chiplet_die_flow.egg-info/PKG-INFO"
expect 0 "evidence: the install's egg-info is allowed" adk_nix_check_evidence
rm -r "$flow/chiplet_die_flow.egg-info"
# chiplet-spec is checked once, with the egg-info of both its install dirs
# allowed, and none outside them.
spec="$at/staging/chiplet-spec"
mkdir -p "$spec/reference/python/chiplet_format_io.egg-info" \
         "$spec/description/src/chiplet_system_description.egg-info"
touch "$spec/reference/python/chiplet_format_io.egg-info/PKG-INFO" \
      "$spec/description/src/chiplet_system_description.egg-info/PKG-INFO"
expect 0 "evidence: the egg-info of both chiplet-spec install dirs is allowed" adk_nix_check_evidence
mkdir "$spec/stray.egg-info"
touch "$spec/stray.egg-info/PKG-INFO"
expect 1 "evidence: an egg-info outside chiplet-spec's install dirs" adk_nix_check_evidence
rm -r "$spec/stray.egg-info" "$spec/reference/python/chiplet_format_io.egg-info" \
      "$spec/description/src/chiplet_system_description.egg-info"
mkdir "$flow/__pycache__"
touch "$flow/__pycache__/stale.cpython-313.pyc"
expect 1 "evidence: ignored bytecode in a staged tree" adk_nix_check_evidence
rm -r "$flow/__pycache__"
mkdir "$at/tools/adk/__pycache__"
touch "$at/tools/adk/__pycache__/stale.cpython-313.pyc"
expect 1 "evidence: ignored bytecode in a root" adk_nix_check_evidence
rm -r "$at/tools/adk/__pycache__"

mv "$at/staging/chiplet-spec" "$work/spec-away"
expect 1 "evidence: staged tree missing" adk_nix_check_evidence
mv "$work/spec-away" "$at/staging/chiplet-spec"
lvs="$at/staging/assembly-lvs"
git -C "$lvs" commit -q --allow-empty -m "moved"
expect 1 "evidence: assembly-lvs at another commit than STAGE_ASSEMBLY_LVS_REF" adk_nix_check_evidence
git -C "$lvs" reset -q --hard HEAD~1
cosim="$at/staging/chiplet-cosim"
git -C "$cosim" commit -q --allow-empty -m "moved"
expect 1 "evidence: chiplet-cosim at another commit than STAGE_CHIPLET_COSIM_REF" adk_nix_check_evidence
git -C "$cosim" reset -q --hard HEAD~1

# adk-tools itself is checked with submodules ignored, so a moved or dirty
# root is caught by its own check against the gitlink in HEAD, not by the
# parent's status.
echo "# edit" >> "$at/tools/adk/pyproject.toml"
expect 1 "evidence: root dirty" adk_nix_check_evidence
git -C "$at/tools/adk" commit -q -am "moved"
expect 1 "evidence: root at another commit" adk_nix_check_evidence
git -C "$at" add tools/adk
expect 1 "evidence: root moved and staged in adk-tools (HEAD is the pin)" adk_nix_check_evidence
git -C "$at" reset -q
git -C "$at/tools/adk" reset -q --hard HEAD~1
expect 0 "evidence: restored" adk_nix_check_evidence

# ---------------------------------------------------------------- venv

(
    ADK_NIX_STATE="$work/state"
    ADK_NIX_PYTHON=/nix/store/python/bin/python3
    ADK_NIX_SITE_PACKAGES=/nix/store/env/lib/python3.13/site-packages
    ADK_NIX_MODE=dev
    d1="$(adk_nix_venv_dir "some trees")"
    expect_eq "dev: the venv is reused" "$(adk_nix_venv_dir "some trees")" "$d1"
    expect 0 "dev: the venv is under the state directory" [ "${d1#"$work"/state/venv-}" != "$d1" ]
    expect 0 "dev: other trees, other venv" [ "$(adk_nix_venv_dir "other trees")" != "$d1" ]
    # Two clones of one commit, as the gate makes them. Evidence keys a tree by
    # its name, install directory and commit; dev by its path.
    git clone -q "$src/chiplet-cosim" "$work/clone-a"
    git clone -q "$src/chiplet-cosim" "$work/clone-b"
    ta="chiplet-cosim|$work/clone-a|."
    tb="chiplet-cosim|$work/clone-b|."
    expect 0 "dev: two clones, two venvs" [ "$(adk_nix_venv_dir "$ta")" != "$(adk_nix_venv_dir "$tb")" ]
    da="$(adk_nix_venv_dir "$ta")"
    ADK_NIX_MODE=evidence
    e1="$(adk_nix_venv_dir "$ta")"
    expect 0 "evidence: the venv is under the state directory" \
        [ "${e1#"$work"/state/evidence-venv-}" != "$e1" ]
    # The same path every time: the flow records the interpreter's path, and
    # resume refuses a changed one.
    expect_eq "evidence: the same path every entry" "$(adk_nix_venv_dir "$ta")" "$e1"
    expect_eq "evidence: two clones of one commit, one venv" "$(adk_nix_venv_dir "$tb")" "$e1"
    expect 0 "evidence: never the dev venv" [ "$e1" != "$da" ]
    git -C "$work/clone-b" commit -q --allow-empty -m "moved"
    expect 0 "evidence: another commit, another venv" [ "$(adk_nix_venv_dir "$tb")" != "$e1" ]
    git -C "$work/clone-b" reset -q --hard HEAD~1
    expect 0 "evidence: another install directory, another venv" \
        [ "$(adk_nix_venv_dir "chiplet-cosim|$work/clone-a|src")" != "$e1" ]
    expect 1 "evidence: a tree whose HEAD cannot be read refuses" \
        adk_nix_venv_dir "chiplet-cosim|$work/state|."

    # The claim, through a real interpreter: the venv emptied, and its lock held
    # by this shell until it closes the descriptor.
    ADK_NIX_PYTHON="$test_python"
    mkdir -p "$e1"
    touch "$e1/left-behind"
    adk_nix_venv_claim "$e1"
    expect_eq "evidence: the claim empties the venv" "$(ls -A "$e1")" ""
    touch "$e1/in-use"
    expect 1 "evidence: a second claim on a held venv refuses" adk_nix_venv_claim "$e1"
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "evidence: that refusal names the lock" \
        grep -qF "$e1 is held by another evidence shell ($e1.lock)" "$work/refusal.out"
    expect 0 "evidence: and leaves the venv in use as it was" test -e "$e1/in-use"
    exec {ADK_NIX_VENV_LOCK}>&-
    expect 0 "evidence: once its holder closes the lock, the venv can be claimed" \
        adk_nix_venv_claim "$e1"
    ADK_NIX_STATE=
    expect 1 "no state directory refuses" adk_nix_venv_dir "some trees"
    echo "$passed $failed" > "$work/counts"
)
read -r passed failed < "$work/counts"

# ---------------------------------------------------------------- closure

# A stand-in store under $work, named the way a real one is, and the
# store-paths of a closureInfo listing what a shell provides: the interpreter
# env, git and the co-simulation devshell. The checks read nothing else. The
# wide closure also lists an env named like the pin's and a second devshell, so
# a case using either gets past the closure checks to the pin check it tests.
ADK_NIX_STORE="$rwork/store"
s_env="$ADK_NIX_STORE/0000000000000000000000000000000e-python3-3.13.9-env"
s_git="$ADK_NIX_STORE/0000000000000000000000000000000g-git-2.51.0"
s_dev="$ADK_NIX_STORE/0000000000000000000000000000000c-cosim-devshell"
s_dev2="$ADK_NIX_STORE/0000000000000000000000000000000d-cosim-devshell"
mkdir -p "$s_env/lib/python3.13/site-packages" "$s_env-x/lib" "$s_git/bin" "$s_dev/bin" \
    "$s_dev2/bin" "$ADK_NIX_STORE/short-git/bin" "$work/hostgit"
printf '#!/bin/sh\nexec "%s" "$@"\n' "$host_git" > "$s_git/bin/git"
cp "$s_git/bin/git" "$work/hostgit/git"
chmod +x "$s_git/bin/git" "$work/hostgit/git"
closure="$work/closure"
mkdir -p "$closure" "$work/closure-nogit" "$work/closure-noenv" "$work/closure-nodev" \
    "$work/closure-longer" "$work/closure-wide"
printf '%s\n' "$s_dev" "$s_env" "$s_git" > "$closure/store-paths"
printf '%s\n' "$s_dev" "$s_env" > "$work/closure-nogit/store-paths"
printf '%s\n' "$s_dev" "$s_git" > "$work/closure-noenv/store-paths"
printf '%s\n' "$s_env" "$s_git" > "$work/closure-nodev/store-paths"
printf '%s\n' "$s_dev" "$s_env" "$s_git-doc" > "$work/closure-longer/store-paths"
printf '%s\n' "$s_dev" "$s_dev2" "$s_env" "$s_env-x" "$s_git" > "$work/closure-wide/store-paths"
pinned="$work/pinned.pin"
printf 'NIX_PYTHON_OUTPATH="%s"\nNIX_COSIM_DEVSHELL_OUTPATH="%s"\n' "$s_env" "$s_dev" > "$pinned"
grep '^STAGE_' "$at/staging.pin" >> "$pinned"

(
    ADK_NIX_CLOSURE_INFO="$closure"
    expect_eq "closure: a listed store path gives its file's real path" \
        "$(adk_nix_in_closure git "$s_git/bin/git")" "$s_git/bin/git"
    ln -s "$s_git/bin/git" "$work/git-link"
    expect_eq "closure: a link into the store resolves to the store's file" \
        "$(adk_nix_in_closure git "$work/git-link")" "$s_git/bin/git"
    expect 1 "closure: a file outside the store refuses" adk_nix_in_closure git "$work/hostgit/git"
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "closure: that refusal says where it resolved" \
        grep -qF "git: $work/hostgit/git resolves to $rwork/hostgit/git, which is not in $ADK_NIX_STORE" \
        "$work/refusal.out"
    expect 1 "closure: a store path the closure does not list refuses" \
        adk_nix_in_closure cosim "$s_dev2"
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "closure: that refusal names the store path" \
        grep -qF "cosim: $s_dev2 is not in this shell's closure" "$work/refusal.out"
    expect 1 "closure: a listed path that only starts with it does not count" \
        with_env ADK_NIX_CLOSURE_INFO="$work/closure-longer" adk_nix_in_closure git "$s_git/bin/git"
    expect 1 "closure: a name without the store's hash refuses" \
        adk_nix_in_closure git "$ADK_NIX_STORE/short-git/bin"
    expect 1 "closure: the store itself refuses" adk_nix_in_closure git "$ADK_NIX_STORE"
    expect 1 "closure: a missing path refuses" adk_nix_in_closure git "$s_git/bin/absent"
    expect 1 "closure: a closureInfo without store-paths refuses" \
        with_env ADK_NIX_CLOSURE_INFO="$work/no-closure" adk_nix_in_closure git "$s_git/bin/git"

    ADK_NIX_STAGING_PIN="$pinned"
    ADK_NIX_SITE_PACKAGES="$s_env/lib/python3.13/site-packages"
    ADK_NIX_COSIM_DEVSHELL="$s_dev"
    expect 0 "pinned paths: the env and the devshell are their pins, in the closure" \
        adk_nix_check_pinned_paths
    expect 1 "pinned paths: a site-packages in a sibling named like the pin refuses" \
        with_env ADK_NIX_CLOSURE_INFO="$work/closure-wide" \
        with_env ADK_NIX_SITE_PACKAGES="$s_env-x/lib" adk_nix_check_pinned_paths
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "pinned paths: that refusal names the pin" \
        grep -qF "is not under NIX_PYTHON_OUTPATH $s_env" "$work/refusal.out"
    expect 1 "pinned paths: the env itself is not a site-packages under it" \
        with_env ADK_NIX_SITE_PACKAGES="$s_env" adk_nix_check_pinned_paths
    expect 1 "pinned paths: an env the closure does not list refuses" \
        with_env ADK_NIX_CLOSURE_INFO="$work/closure-noenv" adk_nix_check_pinned_paths
    expect 1 "pinned paths: another devshell refuses" \
        with_env ADK_NIX_CLOSURE_INFO="$work/closure-wide" \
        with_env ADK_NIX_COSIM_DEVSHELL="$s_dev2" adk_nix_check_pinned_paths
    cp "$work/last.out" "$work/refusal.out"
    expect 0 "pinned paths: that refusal names the devshell pin" \
        grep -qF "is not NIX_COSIM_DEVSHELL_OUTPATH $s_dev" "$work/refusal.out"
    expect 1 "pinned paths: a devshell the closure does not list refuses" \
        with_env ADK_NIX_CLOSURE_INFO="$work/closure-nodev" adk_nix_check_pinned_paths
    printf 'NIX_COSIM_DEVSHELL_OUTPATH="%s"\n' "$s_dev" > "$work/nopython.pin"
    expect 1 "pinned paths: a pin file without NIX_PYTHON_OUTPATH refuses" \
        with_env ADK_NIX_STAGING_PIN="$work/nopython.pin" adk_nix_check_pinned_paths
    printf 'NIX_PYTHON_OUTPATH="%s"\n' "$s_env" > "$work/nodevshell.pin"
    expect 1 "pinned paths: a pin file without NIX_COSIM_DEVSHELL_OUTPATH refuses" \
        with_env ADK_NIX_STAGING_PIN="$work/nodevshell.pin" adk_nix_check_pinned_paths
    { echo false; cat "$pinned"; } > "$work/unclean.pin"
    expect 1 "pinned paths: a pin file that does not source cleanly refuses" \
        with_env ADK_NIX_STAGING_PIN="$work/unclean.pin" adk_nix_check_pinned_paths
    expect 1 "pinned paths: a missing pin file refuses" \
        with_env ADK_NIX_STAGING_PIN="$work/absent.pin" adk_nix_check_pinned_paths
    echo "$passed $failed" > "$work/counts"
)
read -r passed failed < "$work/counts"

# ---------------------------------------------------------------- main (dev)

# adk_nix_main end to end in dev mode, with stand-ins for the interpreter, the
# venv's python, openroad and klayout. Nothing the hook prints may reach stdout, which
# belongs to the command the shell was entered for; a failing pip check refuses
# with pip's own line on stderr and leaves no stamp, so the next entry checks
# again.
stub="$work/stub"
mkdir -p "$stub/openroad/bin" "$stub/klayout/bin" "$work/tmp" "$work/roots/adk" \
    "$work/roots/pdk/libs.tech/klayout" "$work/roots/ic" "$work/roots/pdk-bare"
cat > "$stub/python" <<'EOF'
#!/usr/bin/env bash
# The interpreter, as the hook calls it: `-m venv --without-pip <dir>` here;
# the tool table's writer (-I -B) and its reader (-I -c) through a real python3.
case "$1 ${2:-} ${3:-}" in
    "-m venv --without-pip") mkdir -p "$4/bin" && cp "$(dirname "$0")/venv-python" "$4/bin/python" ;;
    "-I -B "*|"-I -c "*) exec "$STUB_REAL_PYTHON" "$@" ;;
    *) echo "stub python: $*" >&2; exit 2 ;;
esac
EOF
# The table writer at $ADK_NIX_LIB: its arguments one per line, then a table
# holding only a closure digest, or a refusal.
cat > "$stub/tool_identity.py" <<'EOF'
import json, os, sys
with open(os.environ["STUB_TABLE_ARGS"], "w") as handle:
    handle.write("".join(f"{arg}\n" for arg in sys.argv[1:]))
if os.environ.get("STUB_TABLE_FAILS"):
    sys.exit("tool_identity: stub refusal")
out = sys.argv[sys.argv.index("--out") + 1]
with open(out, "w") as handle:
    json.dump({"closure_sha256": os.environ.get("STUB_TABLE_CLOSURE") or "c" * 64}, handle)
print(f"stub  {out}")
EOF
cat > "$stub/venv-python" <<'EOF'
#!/usr/bin/env bash
# The venv's python, as the hook calls it: sysconfig's purelib, pip install,
# pip check. Each answers on stdout, as pip does.
case "$1 ${2:-} ${3:-}" in
    "-c "*) d="$(cd "$(dirname "$0")/.." && pwd)/lib/site"; mkdir -p "$d"; echo "$d" ;;
    "-m pip install")
        # The console script an editable install of assembly-lvs writes.
        case "$*" in
            */assembly-lvs/.*) printf '#!/bin/sh\n' > "$(dirname "$0")/assembly-lvs"
                               chmod +x "$(dirname "$0")/assembly-lvs" ;;
        esac
        echo "stub: pip install $*" ;;
    "-m pip check")
        if [ -n "${STUB_PIP_CHECK_FAILS:-}" ]; then
            echo "stub-dist 1.0 requires missing-dist, which is not installed."
            exit 1
        fi
        echo "No broken requirements found." ;;
    *) echo "stub venv python: $*" >&2; exit 2 ;;
esac
EOF
printf '#!/usr/bin/env bash\necho stub-openroad\n' > "$stub/openroad/bin/openroad"
printf '#!/usr/bin/env bash\necho KLayout 0.30.6\n' > "$stub/klayout/bin/klayout"
chmod +x "$stub/python" "$stub/venv-python" "$stub/openroad/bin/openroad" "$stub/klayout/bin/klayout"

# What KLayout would read if the hook left it, and CHIPLET_FLOW_ASSEMBLY_LVS,
# which lvs-verify runs ahead of PATH: each is set before the entry and must be
# gone after it.
klayout_steering="LD_LIBRARY_PATH RUBYOPT RUBYLIB KLAYOUT_PYTHONPATH KLAYOUT_PYTHONHOME
KLAYOUT_IGNORE_MACRO_DIRS KLAYOUT_SALT_MINE KLAYOUT_VERBOSITY KLAYOUT_PATH KLAYOUT_HOME
CHIPLET_FLOW_PADMAP_PYTHON CHIPLET_FLOW_ASSEMBLY_LVS"

# What packaging-flow and chiplet-cosim would read if the hook left it: each is
# set before the entry, and must be gone or set by the hook after it.
tools_env="ADK_TOOLS_TABLE ADK_TOOLS_CLOSURE ADK_TOOLS_IMAGE COSIM_DEVSHELL"

# arg_after <file> <flag>: the argument the table writer was given after flag.
arg_after() {
    awk -v f="$2" 'prev == f { print; exit } { prev = $0 }' "$1"
}

# An assembly-lvs on the caller's PATH, which lvs-verify must not reach.
mkdir -p "$work/hostbin"
printf '#!/bin/sh\n' > "$work/hostbin/assembly-lvs"
chmod +x "$work/hostbin/assembly-lvs"

# enter <state dir> [fail]: one dev entry, in a subshell because the hook exits
# on a refusal. stdout, stderr and the exit status go to $work/main.*, and the
# variables above and the assembly-lvs on PATH as the entry left them to
# $work/main.env.
# ENTER_PDK and ENTER_KLAYOUT replace the interposer root and the klayout
# output for one entry; ENTER_EDITABLE the table of trees; ENTER_SITE, ENTER_DEVSHELL, ENTER_PIN and ENTER_CLOSURE
# the interpreter env's site-packages, the devshell, the pin file and the
# closureInfo; ENTER_GIT the directory first on PATH, where the shell's git is;
# ENTER_TABLE_FAILS and ENTER_TABLE_CLOSURE make the table writer refuse or
# write that closure digest. TMPDIR ends in a slash, as a caller's may.
enter() {
    (
        unset ADK_NIX_SRC_CHIPLET_FLOW ADK_NIX_SRC_PACKAGING_FLOW \
              ADK_NIX_SRC_INTERPOSER_PNR ADK_NIX_SRC_CHIPLET_SPEC ADK_NIX_SRC_ASSEMBLY_LVS \
              ADK_NIX_SRC_CHIPLET_COSIM
        ADK_NIX_EDITABLE="${ENTER_EDITABLE-$ADK_NIX_EDITABLE}"
        export PATH="${ENTER_GIT-$s_git/bin}:$work/hostbin:$PATH"
        [ -z "${2:-}" ] || export STUB_PIP_CHECK_FAILS=1
        export ADK_NIX_MODE=dev ADK_NIX_STATE="$1" ADK_NIX_SOURCES="$src" TMPDIR="$work/tmp/"
        export ADK_NIX_PYTHON="$stub/python"
        export ADK_NIX_SITE_PACKAGES="${ENTER_SITE-$s_env/lib/python3.13/site-packages}"
        export ADK_NIX_OPENROAD="$stub/openroad" ADK_NIX_CLOSURE_INFO="${ENTER_CLOSURE-$closure}"
        export ADK_NIX_KLAYOUT="${ENTER_KLAYOUT-$stub/klayout}" ADK_NIX_KLAYOUT_IMAGE_VERSION=0.30.5
        export ADK_NIX_FLAKE_REV=dev ADK_NIX_FLAKE_LOCK="$stub/flake.lock" ADK_NIX_LIB="$stub"
        export ADK_NIX_COSIM_DEVSHELL="${ENTER_DEVSHELL-$s_dev}" ADK_NIX_STAGING_PIN="${ENTER_PIN-$pinned}"
        export ADK_ROOT="$work/roots/adk" INTERPOSER_PDK_ROOT="${ENTER_PDK:-$work/roots/pdk}" \
               INTERCONNECT_PDK_ROOT="$work/roots/ic"
        export STUB_REAL_PYTHON="$test_python" STUB_TABLE_ARGS="$work/main.table-args" \
               STUB_TABLE_FAILS="${ENTER_TABLE_FAILS:-}" STUB_TABLE_CLOSURE="${ENTER_TABLE_CLOSURE:-}"
        rm -f "$work/main.table-args"
        for v in $klayout_steering $tools_env; do
            export "$v=/host/$v"
        done
        adk_nix_main
        for v in $klayout_steering $tools_env; do
            printf '%s=%s\n' "$v" "${!v-<unset>}"
        done > "$work/main.env"
        printf 'ADK_NIX_VENV=%s\n' "$ADK_NIX_VENV" >> "$work/main.env"
        printf 'ADK_NIX_TMP=%s\n' "$ADK_NIX_TMP" >> "$work/main.env"
        printf 'ASSEMBLY_LVS=%s\n' "$(command -v assembly-lvs)" >> "$work/main.env"
        printf 'CHIPLET_SPEC_ROOT=%q\n' "$CHIPLET_SPEC_ROOT" >> "$work/main.env"
    ) >"$work/main.out" 2>"$work/main.err"
    echo $? > "$work/main.rc"
}

# The value the last entry left for one variable.
entered() {
    sed -n "s|^$1=||p" "$work/main.env"
}

enter "$work/main-ok"
expect_eq "main: a dev entry succeeds" "$(cat "$work/main.rc")" 0
expect 1 "main: nothing reaches stdout, not even a newline" test -s "$work/main.out"
expect 0 "main: pip install's output is on stderr" grep -q "stub: pip install" "$work/main.err"
expect 0 "main: pip check's output is on stderr" grep -q "No broken requirements found." "$work/main.err"
expect 0 "main: the stamp is written" test -s "$(ls -d "$work"/main-ok/venv-*)/.adk-nix-stamp"
expect 0 "main: both chiplet-spec install dirs are installed" \
    grep -qF -- "-e $src/chiplet-spec/reference/python -e $src/chiplet-spec/description" "$work/main.err"
expect_eq "main: CHIPLET_SPEC_ROOT is the one spec tree" "$(entered CHIPLET_SPEC_ROOT)" "$src/chiplet-spec"

# KLayout, as the entry leaves it: the steering variables gone, the home new and
# empty, the search path the image's shape built from the declared root, and the
# flow's klayout.db interpreter named as this venv's.
for v in LD_LIBRARY_PATH RUBYOPT RUBYLIB KLAYOUT_PYTHONPATH KLAYOUT_PYTHONHOME \
         KLAYOUT_IGNORE_MACRO_DIRS KLAYOUT_SALT_MINE KLAYOUT_VERBOSITY; do
    expect_eq "main: $v is scrubbed" "$(entered "$v")" "<unset>"
done
kl_home="$(entered KLAYOUT_HOME)"
expect 0 "main: KLAYOUT_HOME is the entry's own" \
    bash -c '[[ "$1" == "$2"/adk-nix.*/klayout ]]' _ "$kl_home" "$rwork/tmp"
expect 0 "main: KLAYOUT_HOME is empty" bash -c '[ -d "$1" ] && [ -z "$(ls -A "$1")" ]' _ "$kl_home"
expect_eq "main: KLAYOUT_PATH is the home, then the interposer tree" "$(entered KLAYOUT_PATH)" \
    "$kl_home:$(realpath "$work/roots/pdk")/libs.tech/klayout"
expect_eq "main: the klayout.db interpreter is the venv's" "$(entered CHIPLET_FLOW_PADMAP_PYTHON)" \
    "$(entered ADK_NIX_VENV)/bin/python"
expect 0 "main: the ready line names KLayout" grep -q "KLayout $stub/klayout" "$work/main.err"
# lvs-verify runs CHIPLET_FLOW_ASSEMBLY_LVS if set, else `assembly-lvs` from PATH.
expect_eq "main: CHIPLET_FLOW_ASSEMBLY_LVS is scrubbed" "$(entered CHIPLET_FLOW_ASSEMBLY_LVS)" "<unset>"
expect_eq "main: assembly-lvs on PATH is the venv's, not the caller's" "$(entered ASSEMBLY_LVS)" \
    "$(entered ADK_NIX_VENV)/bin/assembly-lvs"
# The entry's directory, resolved: a TMPDIR ending in a slash leaves no double
# slash in it or in anything built on it.
expect 0 "main: the entry's directory is resolved" \
    bash -c '[[ "$1" == "$2"/tmp/adk-nix.* && "$1" != *//* ]]' _ "$(entered ADK_NIX_TMP)" "$rwork"
# The state directory, resolved before the venv is keyed: one directory under
# two spellings is one venv.
venv_ok="$(entered ADK_NIX_VENV)"
expect 0 "main: the venv is under the resolved state directory" \
    bash -c '[[ "$1" == "$2"/main-ok/venv-* ]]' _ "$venv_ok" "$rwork"
# The tool identity table: written from the closureInfo and the pins, git by
# its store file and --version, and no chiplet-cosim row in dev.
expect_eq "main: ADK_TOOLS_TABLE is the entry's table" "$(entered ADK_TOOLS_TABLE)" \
    "$(entered ADK_NIX_TMP)/tool-identity.json"
expect 0 "main: the table is written" test -s "$(entered ADK_TOOLS_TABLE)"
expect_eq "main: ADK_TOOLS_CLOSURE is sha256: and the table's closure digest" \
    "$(entered ADK_TOOLS_CLOSURE)" "sha256:$(printf 'c%.0s' {1..64})"
expect_eq "main: the writer is tool_identity.py lane" "$(head -n 1 "$work/main.table-args")" lane
expect_eq "main: the table is written from the shell's closureInfo" \
    "$(arg_after "$work/main.table-args" --closure-info)" "$closure"
expect_eq "main: and from the shell's pins" "$(arg_after "$work/main.table-args" --pin)" "$pinned"
expect_eq "main: git is a row, by its store file and --version" \
    "$(arg_after "$work/main.table-args" --tool)" "git=$("$host_git" --version):$s_git/bin/git"
expect_eq "main: dev leaves chiplet-cosim's row out" \
    "$(grep -cx -- --tracked-tool "$work/main.table-args")" 0
expect_eq "main: ADK_TOOLS_IMAGE is scrubbed" "$(entered ADK_TOOLS_IMAGE)" "<unset>"
expect_eq "main: COSIM_DEVSHELL is the shell's devshell" "$(entered COSIM_DEVSHELL)" "$s_dev"
ln -s main-ok "$work/state-link"
enter "$work/state-link"
expect_eq "main: a link to the state directory gives the same venv" "$(entered ADK_NIX_VENV)" "$venv_ok"

ENTER_SITE="$s_env" enter "$work/main-ok"
expect_eq "main: a site-packages that is not under the pin refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names NIX_PYTHON_OUTPATH" \
    grep -q "is not under NIX_PYTHON_OUTPATH $s_env" "$work/main.err"
ENTER_CLOSURE="$work/closure-noenv" enter "$work/main-ok"
expect_eq "main: an env the closure does not list refuses" "$(cat "$work/main.rc")" 1
ENTER_DEVSHELL="$s_dev2" enter "$work/main-ok"
expect_eq "main: a devshell other than the pin refuses" "$(cat "$work/main.rc")" 1
ENTER_DEVSHELL= enter "$work/main-ok"
expect_eq "main: no ADK_NIX_COSIM_DEVSHELL refuses" "$(cat "$work/main.rc")" 1
# A row added to a copy of the table, whose ref the pin does not set.
ENTER_EDITABLE="$ADK_NIX_EDITABLE"$'\n'"chiplet-extra|STAGE_CHIPLET_EXTRA_REF|." enter "$work/main-ok"
expect_eq "main: a table row whose ref the pin does not set refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names the row" \
    grep -q "staging.pin has no STAGE_CHIPLET_EXTRA_REF for chiplet-extra" "$work/main.err"
ENTER_EDITABLE="$ADK_NIX_EDITABLE"$'\n'"chiplet-spec|STAGE_CHIPLET_FLOW_REF|other" enter "$work/main-ok"
expect_eq "main: rows of one tree naming two refs refuse" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names both" \
    grep -qF "chiplet-spec: its rows name both STAGE_CHIPLET_SPEC_REF and STAGE_CHIPLET_FLOW_REF" "$work/main.err"
ENTER_PIN= enter "$work/main-ok"
expect_eq "main: no ADK_NIX_STAGING_PIN refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names it" grep -q "ADK_NIX_STAGING_PIN is not set" "$work/main.err"
ENTER_GIT="$work/hostgit" enter "$work/main-ok"
expect_eq "main: a git outside the store refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names git" grep -q "git: $work/hostgit/git resolves to" "$work/main.err"
ENTER_CLOSURE="$work/closure-nogit" enter "$work/main-ok"
expect_eq "main: a git the closure does not list refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names git" grep -q "git: $s_git is not in this shell's closure" "$work/main.err"
ENTER_TABLE_FAILS=1 enter "$work/main-ok"
expect_eq "main: a table the writer refuses refuses the entry" "$(cat "$work/main.rc")" 1
expect 0 "main: with the writer's line" grep -q "tool_identity: stub refusal" "$work/main.err"
expect 0 "main: and the hook's" grep -q "the tool identity table was not written" "$work/main.err"
expect 1 "main: that refusal prints nothing to stdout" test -s "$work/main.out"
ENTER_TABLE_CLOSURE=not-a-digest enter "$work/main-ok"
expect_eq "main: a table whose closure digest is not a sha256 refuses" "$(cat "$work/main.rc")" 1

# The table's rows in evidence mode, by the function alone: chiplet-cosim joins
# git, by its console script and its staged checkout.
(
    ADK_NIX_MODE=evidence ADK_NIX_PYTHON="$stub/python" ADK_NIX_LIB="$stub"
    ADK_NIX_CLOSURE_INFO="$closure" ADK_NIX_STAGING_PIN="$pinned" ADK_NIX_TMP="$work/table-tmp"
    export STUB_REAL_PYTHON="$test_python" STUB_TABLE_ARGS="$work/table.args" STUB_TABLE_FAILS= \
           STUB_TABLE_CLOSURE=
    mkdir -p "$ADK_NIX_TMP"
    PATH="$s_git/bin:$PATH"
    trees="chiplet-flow|$src/chiplet-flow|."$'\n'"chiplet-cosim|$work/clone-a|."
    expect 0 "table: an evidence entry writes it" adk_nix_tool_table "$work/venv-x" "$trees"
    expect_eq "table: evidence adds chiplet-cosim by its console script and staged checkout" \
        "$(arg_after "$work/table.args" --tracked-tool)" \
        "chiplet-cosim=$work/venv-x/bin/chiplet-cosim:$work/clone-a"
    expect_eq "table: and keeps git" "$(arg_after "$work/table.args" --tool)" \
        "git=$("$host_git" --version):$s_git/bin/git"
    expect 1 "table: a ':' in the venv's path refuses" adk_nix_tool_table "$work/venv:x" "$trees"
    expect 1 "table: a git outside the store refuses" \
        with_env PATH="$work/hostgit:$PATH" adk_nix_tool_table "$work/venv-x" "$trees"
    echo "$passed $failed" > "$work/counts"
)
read -r passed failed < "$work/counts"

ENTER_PDK="$work/roots/pdk-bare" enter "$work/main-ok"
expect_eq "main: an interposer root without libs.tech/klayout refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names KLAYOUT_PATH" \
    grep -q "has no libs.tech/klayout for KLAYOUT_PATH" "$work/main.err"
ENTER_KLAYOUT="$work/roots/ic" enter "$work/main-ok"
expect_eq "main: a KLayout output without bin/klayout refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names klayout" grep -q "no klayout at $work/roots/ic/bin/klayout" "$work/main.err"
ENTER_KLAYOUT= enter "$work/main-ok"
expect_eq "main: no ADK_NIX_KLAYOUT refuses" "$(cat "$work/main.rc")" 1
expect 0 "main: that refusal names it" grep -q "ADK_NIX_KLAYOUT is not set" "$work/main.err"
enter "$work/main-ok"
expect_eq "main: the same entry, put back, succeeds" "$(cat "$work/main.rc")" 0

enter "$work/main-broken" fail
expect_eq "main: a failing pip check refuses" "$(cat "$work/main.rc")" 1
expect 1 "main: a refusal prints nothing to stdout" test -s "$work/main.out"
expect 0 "main: pip check's own line is on stderr" \
    grep -q "requires missing-dist, which is not installed" "$work/main.err"
expect 0 "main: the refusal names pip check" grep -q "adk-nix: pip check failed in" "$work/main.err"
expect 1 "main: a failed check leaves no stamp" \
    test -e "$(ls -d "$work"/main-broken/venv-*)/.adk-nix-stamp"

# ---------------------------------------------------------------- stamp

ADK_NIX_PYTHON=/nix/store/python/bin/python3
ADK_NIX_SITE_PACKAGES=/nix/store/env/lib/python3.13/site-packages
trees="$(for name in chiplet-flow packaging-flow interposer-pnr; do echo "$name|$src/$name|."; done
         echo "chiplet-spec|$src/chiplet-spec|reference/python"
         echo "chiplet-spec|$src/chiplet-spec|description")"
stamp0="$(adk_nix_stamp_from "$trees")"
expect_eq "stamp is stable" "$(adk_nix_stamp_from "$trees")" "$stamp0"
echo "x = 1" > "$src/chiplet-flow/module.py"
expect_eq "a Python edit costs no install" "$(adk_nix_stamp_from "$trees")" "$stamp0"
git -C "$src/chiplet-flow" add module.py
git -C "$src/chiplet-flow" commit -q -m "python only"
expect_eq "a committed Python edit costs no install" "$(adk_nix_stamp_from "$trees")" "$stamp0"
echo 'version = "0.2.0"' >> "$src/chiplet-flow/pyproject.toml"
stamp1="$(adk_nix_stamp_from "$trees")"
expect 0 "a metadata edit reinstalls" [ "$stamp1" != "$stamp0" ]
mkdir -p "$src/chiplet-spec/reference/python/chiplet_format_io"
echo '__version__ = "1.0"' > "$src/chiplet-spec/reference/python/chiplet_format_io/__init__.py"
stamp2="$(adk_nix_stamp_from "$trees")"
expect 0 "a chiplet-format-io version edit reinstalls" [ "$stamp2" != "$stamp1" ]
printf '[project]\nname = "chiplet-system-description"\n' > "$src/chiplet-spec/description/pyproject.toml"
stamp3="$(adk_nix_stamp_from "$trees")"
expect 0 "a metadata edit in chiplet-spec's second install dir reinstalls" [ "$stamp3" != "$stamp2" ]
ADK_NIX_SITE_PACKAGES=/nix/store/other-env/lib/python3.13/site-packages
expect 0 "another Nix environment reinstalls" [ "$(adk_nix_stamp_from "$trees")" != "$stamp3" ]

# ---------------------------------------------------------------- openroad pin

# check-openroad-pin.sh against a throwaway staging.pin, with a stand-in for
# nix that prints what the flake would evaluate each package to and records
# how it was asked. The green case holds every pin; each refusal breaks
# exactly one thing.
orp="$work/openroad-pin"
mkdir -p "$orp/at" "$orp/bin"
cat > "$orp/bin/nix" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$STUB_NIX_ARGS"
[ -z "${STUB_NIX_FAILS:-}" ] || { echo "error: stub evaluation failed" >&2; exit 1; }
case "$*" in
    *.openroad.outPath) printf '%s' "$STUB_NIX_OUT" ;;
    *.librelane.outPath) printf '%s' "$STUB_NIX_LIBRELANE" ;;
    *.librelane-python.outPath) printf '%s' "$STUB_NIX_LIBRELANE_PYTHON" ;;
    *.sg13g2-pdk.outPath) printf '%s' "$STUB_NIX_PDK" ;;
    *.python.outPath) printf '%s' "$STUB_NIX_LANE_PYTHON" ;;
    *.cosim-iverilog.outPath) printf '%s' "$STUB_NIX_IVERILOG" ;;
    *.cosim-cocotb.outPath) printf '%s' "$STUB_NIX_COCOTB" ;;
    *.cosim-python.outPath) printf '%s' "$STUB_NIX_PYTHON" ;;
    *.cosim-devshell.outPath) printf '%s' "$STUB_NIX_DEVSHELL" ;;
    *.mixsim-ngspice.outPath) printf '%s' "$STUB_NIX_NGSPICE" ;;
    *.mixsim-iverilog.outPath) printf '%s' "$STUB_NIX_MIXSIM_IVERILOG" ;;
    *.sg13g2-osdi.outPath) printf '%s' "$STUB_NIX_OSDI" ;;
    *.gtkwave.outPath) printf '%s' "$STUB_NIX_GTKWAVE" ;;
    *.gaw.outPath) printf '%s' "$STUB_NIX_GAW" ;;
    *.xschem.outPath) printf '%s' "$STUB_NIX_XSCHEM" ;;
    *.vams-gnucap.outPath) printf '%s' "$STUB_NIX_GNUCAP" ;;
    *.vams-devshell.outPath) printf '%s' "$STUB_NIX_VAMS_DEVSHELL" ;;
esac
EOF
chmod +x "$orp/bin/nix"
good=/nix/store/ihqbw193x4qgdal58v4fczbaq1j3pc03-openroad-2026-02-17
other=/nix/store/7g30wn9qnkcvnkk5h9q1cqk0yl86qxil-openroad
lgood=/nix/store/8bj13r4a4wwmsdgpf47z5v93fhm5jyjl-python3.13-librelane-3.0.0rc0
lother=/nix/store/5yvwlj88pk4rg9nc1grfq01lz1kpgyqx-python3.13-librelane-3.0.0rc0
lpgood=/nix/store/jys2hxxpx11x2nbv3rvmhaks224nixmq-python3-3.13.9-env
lpother=/nix/store/0000000000000000000000000000000f-python3-3.13.9-env
pgood=/nix/store/hx96lf7n4m0if0nl48m5ydj90rvga8z2-ihp-open-pdk-22f4335
pother=/nix/store/1kqgb61618s1lcdggdzbwbabpxz0yhy6-ihp-open-pdk-1111111
xgood=/nix/store/brfq48k67ziycvjr0kk1991acrfqri69-python3-3.13.9-env
igood=/nix/store/nknbcs56x54kqhbv0slfca6v4gmi724v-iverilog-s20250103-60-gdb82380ce
cgood=/nix/store/yk336mn39lfjiawz9zq4qbgq29vhd694-python3.13-cocotb-2.0.0
cother=/nix/store/0000000000000000000000000000000a-python3.13-cocotb-2.0.0
ygood=/nix/store/1czlxfa90k7h85aq923imkr9x2fjmz5v-python3-3.13.9-env
dgood=/nix/store/kcxwxk9fj86r0l2ih49hk3gihk6la9sk-cosim-devshell
dother=/nix/store/0000000000000000000000000000000c-cosim-devshell
ngood=/nix/store/mjipfrxz15vc3ls2m9yiy0zw987vp38m-ngspice-45
vgood=/nix/store/r95n1sqiyfmpnw4jy3znxb91a8khy84g-iverilog-libvvp-s20250103-60-gdb82380ce
ogood=/nix/store/anybmg3w0nlzrl57xms9r9hpai93x4sl-sg13g2-osdi-22f4335
oother=/nix/store/0000000000000000000000000000000b-sg13g2-osdi-22f4335
wgood=/nix/store/8kygk1ymkq6dqym0xv6hy08bxw3px6dv-gtkwave-3.3.121
agood=/nix/store/7gjgfdhglwb682gdv0qmqqxsskkdqlh4-gaw3-20250128
aother=/nix/store/0000000000000000000000000000000d-gaw3-20250128
xsgood=/nix/store/af01dvh78hfmxljwq4v4lm56apxp25hh-xschem-3.4.8RC-8d2088e
xsother=/nix/store/0000000000000000000000000000000f-xschem-3.4.7RC-0000000
ggood=/nix/store/yawvmxraq2bapnsapsb5c2j6j0c856b4-gnucap-b6678fe-modelgen-verilog-100e746
gother=/nix/store/0000000000000000000000000000000e-gnucap-b6678fe-modelgen-verilog-100e746
sgood=/nix/store/1dlfq2bbq2db4dc3bx897zxacf7jbkga-vams-devshell
# pin_line <line...>: a staging.pin holding a staged entry, these lines, and
# the LibreLane, its interpreter, PDK, lane interpreter, engine and viewer lines
# (LL_LINE, LP_LINE, PDK_LINE, PY_LINE, COSIM_LINES, MIXSIM_LINES, VIEWER_LINES and VAMS_LINES;
# empty leaves them out).
LL_LINE="NIX_LIBRELANE_OUTPATH=\"$lgood\""
LP_LINE="NIX_LIBRELANE_PYTHON_OUTPATH=\"$lpgood\""
PDK_LINE="NIX_SG13G2_PDK_OUTPATH=\"$pgood\""
PY_LINE="NIX_PYTHON_OUTPATH=\"$xgood\""
COSIM_LINES="NIX_COSIM_IVERILOG_OUTPATH=\"$igood\""$'\n'"NIX_COSIM_COCOTB_OUTPATH=\"$cgood\""$'\n'"NIX_COSIM_PYTHON_OUTPATH=\"$ygood\""$'\n'"NIX_COSIM_DEVSHELL_OUTPATH=\"$dgood\""
MIXSIM_LINES="NIX_MIXSIM_NGSPICE_OUTPATH=\"$ngood\""$'\n'"NIX_MIXSIM_IVERILOG_OUTPATH=\"$vgood\""$'\n'"NIX_SG13G2_OSDI_OUTPATH=\"$ogood\""
VIEWER_LINES="NIX_GTKWAVE_OUTPATH=\"$wgood\""$'\n'"NIX_GAW_OUTPATH=\"$agood\""$'\n'"NIX_XSCHEM_OUTPATH=\"$xsgood\""
VAMS_LINES="NIX_VAMS_GNUCAP_OUTPATH=\"$ggood\""$'\n'"NIX_VAMS_DEVSHELL_OUTPATH=\"$sgood\""
pin_line() {
    { echo "STAGE_CHIPLET_FLOW_REF=0123"; printf '%s\n' "$@"
      [ -z "$LL_LINE" ] || printf '%s\n' "$LL_LINE"
      [ -z "$LP_LINE" ] || printf '%s\n' "$LP_LINE"
      [ -z "$PDK_LINE" ] || printf '%s\n' "$PDK_LINE"
      [ -z "$PY_LINE" ] || printf '%s\n' "$PY_LINE"
      [ -z "$COSIM_LINES" ] || printf '%s\n' "$COSIM_LINES"
      [ -z "$MIXSIM_LINES" ] || printf '%s\n' "$MIXSIM_LINES"
      [ -z "$VIEWER_LINES" ] || printf '%s\n' "$VIEWER_LINES"
      [ -z "$VAMS_LINES" ] || printf '%s\n' "$VAMS_LINES"; } > "$orp/at/staging.pin"
}
check_pin() {
    rm -f "$orp/args"
    PATH="$orp/bin:$PATH" STUB_NIX_ARGS="$orp/args" STUB_NIX_OUT="${STUB_NIX_OUT-$good}" \
        STUB_NIX_LIBRELANE="${STUB_NIX_LIBRELANE-$lgood}" STUB_NIX_PDK="${STUB_NIX_PDK-$pgood}" \
        STUB_NIX_LIBRELANE_PYTHON="${STUB_NIX_LIBRELANE_PYTHON-$lpgood}" \
        STUB_NIX_LANE_PYTHON="${STUB_NIX_LANE_PYTHON-$xgood}" \
        STUB_NIX_IVERILOG="${STUB_NIX_IVERILOG-$igood}" STUB_NIX_COCOTB="${STUB_NIX_COCOTB-$cgood}" \
        STUB_NIX_PYTHON="${STUB_NIX_PYTHON-$ygood}" STUB_NIX_DEVSHELL="${STUB_NIX_DEVSHELL-$dgood}" \
        STUB_NIX_NGSPICE="${STUB_NIX_NGSPICE-$ngood}" \
        STUB_NIX_MIXSIM_IVERILOG="${STUB_NIX_MIXSIM_IVERILOG-$vgood}" STUB_NIX_OSDI="${STUB_NIX_OSDI-$ogood}" \
        STUB_NIX_GTKWAVE="${STUB_NIX_GTKWAVE-$wgood}" STUB_NIX_GAW="${STUB_NIX_GAW-$agood}" \
        STUB_NIX_XSCHEM="${STUB_NIX_XSCHEM-$xsgood}" \
        STUB_NIX_GNUCAP="${STUB_NIX_GNUCAP-$ggood}" STUB_NIX_VAMS_DEVSHELL="${STUB_NIX_VAMS_DEVSHELL-$sgood}" \
        "$here/check-openroad-pin.sh" "$orp/at" 2> "$orp/err"
}
asked() {
    echo "eval --raw --max-jobs 0 --no-update-lock-file --option allow-import-from-derivation false $orp/at#packages.x86_64-linux.$1.outPath"
}

pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 0 "openroad pin: the flake's paths pass" check_pin
expect_eq "openroad pin: each asked by evaluation only, of the outPath, on the committed lock" \
    "$(cat "$orp/args")" \
    "$(asked openroad; asked librelane; asked librelane-python; asked sg13g2-pdk; asked python
       asked cosim-iverilog; asked cosim-cocotb; asked cosim-python
       asked cosim-devshell; asked mixsim-ngspice; asked mixsim-iverilog; asked sg13g2-osdi
       asked gtkwave; asked gaw; asked xschem; asked vams-gnucap; asked vams-devshell)"
expect 1 "openroad pin: another store path refuses" with_env STUB_NIX_OUT="$other" check_pin
expect 0 "openroad pin: the refusal names both paths" \
    grep -q "names $good, the flake's packages.x86_64-linux.openroad.outPath is $other" "$orp/err"
expect 1 "openroad pin: a failed evaluation refuses" with_env STUB_NIX_FAILS=1 check_pin
expect 0 "openroad pin: that refusal names the evaluation" grep -q "nix eval of .* failed" "$orp/err"
expect 1 "openroad pin: an empty evaluation refuses" with_env STUB_NIX_OUT= check_pin
pin_line
expect 1 "openroad pin: no pin line refuses" check_pin
expect 0 "openroad pin: that refusal counts the lines" grep -q "0 NIX_OPENROAD_OUTPATH lines" "$orp/err"
pin_line "NIX_OPENROAD_OUTPATH=\"$good\"" "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "openroad pin: two pin lines refuse" check_pin
pin_line "NIX_OPENROAD_OUTPATH=$good"
expect 1 "openroad pin: an unquoted value refuses" check_pin
pin_line "export NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "openroad pin: an exported value refuses" check_pin
pin_line "NIX_OPENROAD_OUTPATH=\"$good/bin/openroad\""
expect 1 "openroad pin: the binary instead of the output path refuses" check_pin
pin_line 'NIX_OPENROAD_OUTPATH="/nix/store/$(echo ihqbw193x4qgdal58v4fczbaq1j3pc03)-openroad"'
expect 1 "openroad pin: a substitution refuses" check_pin
expect 0 "openroad pin: that refusal names the literal" grep -q "is not a quoted" "$orp/err"
pin_line "NIX_OPENROAD_OUTPATH=\"$good\"" "NIX_OPENROAD_OUTPATH+=x"
expect 1 "openroad pin: a later append the literal does not show refuses" check_pin
expect 0 "openroad pin: that refusal names the shell reading" grep -q "other than its literal" "$orp/err"
pin_line "NIX_OPENROAD_OUTPATH=\"$good\"" "return 1"
expect 1 "openroad pin: a valid line in a file that fails to source refuses" check_pin
expect 0 "openroad pin: that refusal names the sourcing" grep -q "does not source cleanly" "$orp/err"
pin_line "false" "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "openroad pin: a failing command before a valid line refuses" check_pin
expect 0 "openroad pin: that refusal names the sourcing too" grep -q "does not source cleanly" "$orp/err"

# LibreLane and the PDK go through the same check; these sample it per pin and
# hold each to its own package's name.
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "librelane pin: another store path refuses" with_env STUB_NIX_LIBRELANE="$lother" check_pin
expect 0 "librelane pin: the refusal names both paths" \
    grep -q "librelane pin: staging.pin names $lgood, the flake's packages.x86_64-linux.librelane.outPath is $lother" "$orp/err"
expect 1 "sg13g2-pdk pin: another store path refuses" with_env STUB_NIX_PDK="$pother" check_pin
expect 0 "sg13g2-pdk pin: the refusal names both paths" \
    grep -q "sg13g2-pdk pin: staging.pin names $pgood, the flake's packages.x86_64-linux.sg13g2-pdk.outPath is $pother" "$orp/err"
expect 1 "sg13g2-pdk pin: an empty evaluation refuses" with_env STUB_NIX_PDK= check_pin
LL_LINE= pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "librelane pin: no pin line refuses" check_pin
expect 0 "librelane pin: that refusal counts the lines" grep -q "0 NIX_LIBRELANE_OUTPATH lines" "$orp/err"
PDK_LINE="export NIX_SG13G2_PDK_OUTPATH=\"$pgood\"" pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "sg13g2-pdk pin: an exported value refuses" check_pin
LL_LINE="NIX_LIBRELANE_OUTPATH=\"$good\"" pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "librelane pin: the OpenROAD path in its place refuses" check_pin
expect 0 "librelane pin: that refusal names the literal" \
    grep -q "NIX_LIBRELANE_OUTPATH in .* is not a quoted /nix/store/<hash>-librelane literal" "$orp/err"
PDK_LINE="NIX_SG13G2_PDK_OUTPATH=\"$lgood\"" pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "sg13g2-pdk pin: the LibreLane path in its place refuses" check_pin
LL_LINE="$LL_LINE"$'\nNIX_LIBRELANE_OUTPATH+=x' pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "librelane pin: a later append the literal does not show refuses" check_pin
expect 0 "librelane pin: that refusal names the shell reading" \
    grep -q "librelane pin: .* sets NIX_LIBRELANE_OUTPATH to something other than its literal" "$orp/err"

# LibreLane's interpreter: the lane's and the co-simulation engine's envs have
# the same store name at other hashes, so the evaluation tells them apart.
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "librelane-python pin: another env at its place refuses" with_env STUB_NIX_LIBRELANE_PYTHON="$lpother" check_pin
expect 0 "librelane-python pin: the refusal names both paths" \
    grep -q "librelane-python pin: staging.pin names $lpgood, the flake's packages.x86_64-linux.librelane-python.outPath is $lpother" "$orp/err"
LP_LINE= pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "librelane-python pin: no pin line refuses" check_pin
expect 0 "librelane-python pin: that refusal counts the lines" grep -q "0 NIX_LIBRELANE_PYTHON_OUTPATH lines" "$orp/err"
LP_LINE="NIX_LIBRELANE_PYTHON_OUTPATH=\"$lpgood/bin/python3.13\"" pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "librelane-python pin: the interpreter file in its place refuses" check_pin

# The lane's interpreter too. The co-simulation engine's python env has the
# same store name and another hash, so again only the evaluation tells them
# apart.
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "python pin: the co-simulation env at its place refuses" with_env STUB_NIX_LANE_PYTHON="$ygood" check_pin
expect 0 "python pin: the refusal names both paths" \
    grep -q "python pin: staging.pin names $xgood, the flake's packages.x86_64-linux.python.outPath is $ygood" "$orp/err"
expect 1 "python pin: an empty evaluation refuses" with_env STUB_NIX_LANE_PYTHON= check_pin
PY_LINE= pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "python pin: no pin line refuses" check_pin
expect 0 "python pin: that refusal counts the lines" grep -q "0 NIX_PYTHON_OUTPATH lines" "$orp/err"
PY_LINE="NIX_PYTHON_OUTPATH=\"$cgood\"" pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "python pin: cocotb in its place refuses" check_pin
expect 0 "python pin: that refusal names the literal" \
    grep -q "NIX_PYTHON_OUTPATH in .* is not a quoted /nix/store/<hash>-python literal" "$orp/err"

# The engine's four go through it too. nixpkgs' own cocotb 2.0.0 has the same
# store name as the pinned one and another hash, so only the evaluation tells
# them apart; the name still keeps one engine path out of another's line.
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "cosim-cocotb pin: the same name at another hash refuses" with_env STUB_NIX_COCOTB="$cother" check_pin
expect 0 "cosim-cocotb pin: the refusal names both paths" \
    grep -q "cosim-cocotb pin: staging.pin names $cgood, the flake's packages.x86_64-linux.cosim-cocotb.outPath is $cother" "$orp/err"
expect 1 "cosim-iverilog pin: an empty evaluation refuses" with_env STUB_NIX_IVERILOG= check_pin
expect 1 "cosim-python pin: another store path refuses" with_env STUB_NIX_PYTHON="$cgood" check_pin
COSIM_LINES="NIX_COSIM_IVERILOG_OUTPATH=\"$igood\""$'\n'"NIX_COSIM_COCOTB_OUTPATH=\"$cgood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "cosim-python pin: no pin line refuses" check_pin
expect 0 "cosim-python pin: that refusal counts the lines" grep -q "0 NIX_COSIM_PYTHON_OUTPATH lines" "$orp/err"
COSIM_LINES="NIX_COSIM_IVERILOG_OUTPATH=\"$igood\""$'\n'"NIX_COSIM_COCOTB_OUTPATH=\"$ygood\""$'\n'"NIX_COSIM_PYTHON_OUTPATH=\"$ygood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "cosim-cocotb pin: the python env in its place refuses" check_pin
expect 0 "cosim-cocotb pin: that refusal names the literal" \
    grep -q "NIX_COSIM_COCOTB_OUTPATH in .* is not a quoted /nix/store/<hash>-cosim-cocotb literal" "$orp/err"
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "cosim-devshell pin: another store path refuses" with_env STUB_NIX_DEVSHELL="$dother" check_pin
expect 0 "cosim-devshell pin: the refusal names both paths" \
    grep -q "cosim-devshell pin: staging.pin names $dgood, the flake's packages.x86_64-linux.cosim-devshell.outPath is $dother" "$orp/err"
COSIM_LINES="NIX_COSIM_IVERILOG_OUTPATH=\"$igood\""$'\n'"NIX_COSIM_COCOTB_OUTPATH=\"$cgood\""$'\n'"NIX_COSIM_PYTHON_OUTPATH=\"$ygood\""$'\n'"NIX_COSIM_DEVSHELL_OUTPATH=\"$ygood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "cosim-devshell pin: the python env in its place refuses" check_pin
expect 0 "cosim-devshell pin: that refusal names the literal" \
    grep -q "NIX_COSIM_DEVSHELL_OUTPATH in .* is not a quoted /nix/store/<hash>-cosim-devshell literal" "$orp/err"
COSIM_LINES="NIX_COSIM_IVERILOG_OUTPATH=\"$igood\""$'\n'"NIX_COSIM_COCOTB_OUTPATH=\"$cgood\""$'\n'"NIX_COSIM_PYTHON_OUTPATH=\"$ygood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "cosim-devshell pin: no pin line refuses" check_pin
expect 0 "cosim-devshell pin: that refusal counts the lines" grep -q "0 NIX_COSIM_DEVSHELL_OUTPATH lines" "$orp/err"

# And the mixed-signal engine's three. The OSDI path is input-addressed, so a
# rebuild with other bytes keeps its name and hash; only nix/sg13g2-osdi.sha256
# (build.sh) tells those apart. Here the evaluation still has to agree.
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "sg13g2-osdi pin: another store path refuses" with_env STUB_NIX_OSDI="$oother" check_pin
expect 0 "sg13g2-osdi pin: the refusal names both paths" \
    grep -q "sg13g2-osdi pin: staging.pin names $ogood, the flake's packages.x86_64-linux.sg13g2-osdi.outPath is $oother" "$orp/err"
expect 1 "mixsim-ngspice pin: an empty evaluation refuses" with_env STUB_NIX_NGSPICE= check_pin
MIXSIM_LINES="NIX_MIXSIM_NGSPICE_OUTPATH=\"$ngood\""$'\n'"NIX_MIXSIM_IVERILOG_OUTPATH=\"$igood\""$'\n'"NIX_SG13G2_OSDI_OUTPATH=\"$ogood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "mixsim-iverilog pin: the co-simulation Icarus in its place refuses" check_pin
expect 0 "mixsim-iverilog pin: that refusal names the literal" \
    grep -q "NIX_MIXSIM_IVERILOG_OUTPATH in .* is not a quoted /nix/store/<hash>-mixsim-iverilog literal" "$orp/err"
MIXSIM_LINES="NIX_MIXSIM_NGSPICE_OUTPATH=\"$ngood\""$'\n'"NIX_MIXSIM_IVERILOG_OUTPATH=\"$vgood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "sg13g2-osdi pin: no pin line refuses" check_pin
expect 0 "sg13g2-osdi pin: that refusal counts the lines" grep -q "0 NIX_SG13G2_OSDI_OUTPATH lines" "$orp/err"

# And the viewers' two.
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "gaw pin: another store path refuses" with_env STUB_NIX_GAW="$aother" check_pin
expect 0 "gaw pin: the refusal names both paths" \
    grep -q "gaw pin: staging.pin names $agood, the flake's packages.x86_64-linux.gaw.outPath is $aother" "$orp/err"
expect 1 "gtkwave pin: an empty evaluation refuses" with_env STUB_NIX_GTKWAVE= check_pin
VIEWER_LINES="NIX_GTKWAVE_OUTPATH=\"$wgood\""$'\n'"NIX_GAW_OUTPATH=\"$wgood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "gaw pin: GTKWave in its place refuses" check_pin
expect 0 "gaw pin: that refusal names the literal" \
    grep -q "NIX_GAW_OUTPATH in .* is not a quoted /nix/store/<hash>-gaw literal" "$orp/err"
VIEWER_LINES="NIX_GAW_OUTPATH=\"$agood\"" pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "gtkwave pin: no pin line refuses" check_pin
expect 0 "gtkwave pin: that refusal counts the lines" grep -q "0 NIX_GTKWAVE_OUTPATH lines" "$orp/err"
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "xschem pin: another store path refuses" with_env STUB_NIX_XSCHEM="$xsother" check_pin
expect 0 "xschem pin: the refusal names both paths" \
    grep -q "xschem pin: staging.pin names $xsgood, the flake's packages.x86_64-linux.xschem.outPath is $xsother" "$orp/err"
VIEWER_LINES="NIX_GTKWAVE_OUTPATH=\"$wgood\""$'\n'"NIX_GAW_OUTPATH=\"$agood\""$'\n'"NIX_XSCHEM_OUTPATH=\"$agood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "xschem pin: gaw in its place refuses" check_pin

# And the Verilog-AMS engine's two.
pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "vams-gnucap pin: another store path refuses" with_env STUB_NIX_GNUCAP="$gother" check_pin
expect 0 "vams-gnucap pin: the refusal names both paths" \
    grep -q "vams-gnucap pin: staging.pin names $ggood, the flake's packages.x86_64-linux.vams-gnucap.outPath is $gother" "$orp/err"
expect 1 "vams-devshell pin: an empty evaluation refuses" with_env STUB_NIX_VAMS_DEVSHELL= check_pin
VAMS_LINES="NIX_VAMS_GNUCAP_OUTPATH=\"$sgood\""$'\n'"NIX_VAMS_DEVSHELL_OUTPATH=\"$sgood\"" \
    pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "vams-gnucap pin: the devshell in its place refuses" check_pin
expect 0 "vams-gnucap pin: that refusal names the literal" \
    grep -q "NIX_VAMS_GNUCAP_OUTPATH in .* is not a quoted /nix/store/<hash>-vams-gnucap literal" "$orp/err"
VAMS_LINES="NIX_VAMS_GNUCAP_OUTPATH=\"$ggood\"" pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 1 "vams-devshell pin: no pin line refuses" check_pin
expect 0 "vams-devshell pin: that refusal counts the lines" grep -q "0 NIX_VAMS_DEVSHELL_OUTPATH lines" "$orp/err"

pin_line "NIX_OPENROAD_OUTPATH=\"$good\""
expect 0 "openroad pin: restored" check_pin

# ---------------------------------------------------------------- osdi sha256

# nix/check-osdi-sha.sh on two made-up model files and the list that names
# them, then on each way the directory or the list can differ.
osd="$work/osdi"
mkdir -p "$osd/dir"
printf 'a' > "$osd/dir/b.osdi"
printf 'b' > "$osd/dir/a_x.osdi"
printf 'c' > "$osd/dir/a.osdi"
(cd "$osd/dir" && sha256sum a.osdi a_x.osdi b.osdi) > "$osd/list"
check_osdi() { "$here/check-osdi-sha.sh" "$osd/dir" "$1" 2> "$osd/err"; }
expect 0 "osdi sha256: the listed files pass" check_osdi "$osd/list"
{ echo "# a header"; cat "$osd/list"; } > "$osd/headed"
expect 0 "osdi sha256: '#' lines are skipped" check_osdi "$osd/headed"
printf 'x' > "$osd/dir/b.osdi"
expect 1 "osdi sha256: other bytes under a listed name refuse" check_osdi "$osd/list"
expect 0 "osdi sha256: that refusal says so" grep -q "are not the ones" "$osd/err"
printf 'a' > "$osd/dir/b.osdi"
printf 'd' > "$osd/dir/c.osdi"
expect 1 "osdi sha256: an unlisted file refuses" check_osdi "$osd/list"
rm "$osd/dir/c.osdi"
head -n 2 "$osd/list" > "$osd/short"
expect 1 "osdi sha256: a listed file left out of the list refuses" check_osdi "$osd/short"
sort -r "$osd/list" > "$osd/unsorted"
expect 1 "osdi sha256: an unsorted list refuses" check_osdi "$osd/unsorted"
expect 0 "osdi sha256: that refusal says so" grep -q "is not sorted by name" "$osd/err"
sed 's/  /  sub\//' "$osd/list" > "$osd/pathed"
expect 1 "osdi sha256: a name with a directory refuses" check_osdi "$osd/pathed"
: > "$osd/empty"
expect 1 "osdi sha256: an empty list refuses" check_osdi "$osd/empty"
expect 1 "osdi sha256: a missing list refuses" check_osdi "$osd/absent"
expect 1 "osdi sha256: a missing directory refuses" \
    "$here/check-osdi-sha.sh" "$osd/absent" "$osd/list"
rm "$osd"/dir/*.osdi
expect 1 "osdi sha256: a directory with no model file refuses" check_osdi "$osd/list"

# ---------------------------------------------------------------- klayout skew

# flake.nix reads the image's KLayout from the Dockerfile's KLAYOUT_PIP line and
# fails to evaluate unless there is exactly one; this holds the Dockerfile to
# the form it reads, without Nix. klayout_pip_lines <Dockerfile>
klayout_pip_lines() {
    local n
    n="$(grep -cE '^ARG KLAYOUT_PIP=[^ ]+$' "$1")"
    echo "$n KLAYOUT_PIP lines"
    [ "$n" -eq 1 ]
}
repo="$(cd "$here/.." && pwd)"
expect 0 "klayout skew: the Dockerfile has one KLAYOUT_PIP line" klayout_pip_lines "$repo/Dockerfile"
grep -v '^ARG KLAYOUT_PIP=' "$repo/Dockerfile" > "$work/Dockerfile.none"
expect 1 "klayout skew: no KLAYOUT_PIP line refuses" klayout_pip_lines "$work/Dockerfile.none"
{ cat "$repo/Dockerfile"; echo "ARG KLAYOUT_PIP=0.30.99"; } > "$work/Dockerfile.two"
expect 1 "klayout skew: a second KLAYOUT_PIP line refuses" klayout_pip_lines "$work/Dockerfile.two"

echo "cli-env: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
