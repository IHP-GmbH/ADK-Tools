#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Tests for nix/pdk-source.nix and nix/sg13g2-pdk.nix on stub inputs: stand-in
# parent and submodule trees with made-up revs and narHashes, never the real
# PDK, so nothing is fetched.
#
# The first part evaluates the statement with nix-instantiate alone. The
# second builds nix/sg13g2-pdk.nix on those stubs with the nixpkgs flake.lock
# names, which must already be in the store: before evaluating anything that
# imports it, it refuses to run when that store path is not valid, and then
# when a dry run would fetch or build anything but its own stub derivations.
# --eval-only skips it (its checks then count as not run, not as passed).
#
# Needs nix, python3 and coreutils. Writes only under one mktemp directory,
# and into the store the stub trees and the trees built from them.
#
# Usage: nix/test-pdk-source.sh [--eval-only]

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
lock="$here/../flake.lock"
eval_only=0
[ "${1:-}" = "--eval-only" ] && eval_only=1

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-pdk-source-test.XXXXXX")"
trap 'chmod -R u+w "$work" 2>/dev/null; rm -rf "$work"' EXIT
work="$(realpath "$work")"

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

# expect_out <description> <pattern>: a line of the last output matches.
expect_out() {
    if grep -q -E -- "$2" "$work/last.out"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        echo "FAIL: $1 (no line matches $2)"
        sed 's/^/    /' "$work/last.out"
    fi
}

rev_p=1111111111111111111111111111111111111111
rev_a=2222222222222222222222222222222222222222
rev_b=3333333333333333333333333333333333333333
nar_p="sha256-$(printf 'p%.0s' $(seq 43))="
nar_a="sha256-$(printf 'a%.0s' $(seq 43))="
nar_b="sha256-$(printf 'b%.0s' $(seq 43))="
sub_a=ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api
sub_b=ihp-sg13g2/libs.tech/klayout/python/pypreprocessor

# The parent's .gitmodules at SG13G2_REF, tab-indented as upstream writes it,
# its entries out of path order.
gitmodules() {
    printf '[submodule "%s"]\n\tpath = %s\n\turl = https://example.invalid/%s\n' \
        ihp-sg13g2/libs.tech/palace ihp-sg13g2/libs.tech/palace p \
        "$sub_b" "$sub_b" b \
        ihp-sg13g2/libs.tech/digital ihp-sg13g2/libs.tech/digital d \
        "$sub_a" "$sub_a" a \
        ihp-sg13g2/libs.tech/openems/openems_ihp_sg13g2 ihp-sg13g2/libs.tech/openems/openems_ihp_sg13g2 o
}

# A stub parent tree; its .gitmodules is gitmodules() unless given.
parent_tree() {
    local d="$work/$1"
    mkdir -p "$d/ihp-sg13g2/libs.tech/klayout/python"
    echo "parent file" >"$d/ihp-sg13g2/README"
    if [ $# -gt 1 ]; then printf '%s' "$2" >"$d/.gitmodules"; else gitmodules >"$d/.gitmodules"; fi
    echo "$d"
}
sub_tree() {
    local d="$work/$1"
    mkdir -p "$d"
    echo "$1" >"$d/marker"
    echo "$d"
}

good_parent="$(parent_tree good)"
sa="$(sub_tree sub-a)"
sb="$(sub_tree sub-b)"

# The arguments pdk-source.nix takes, as JSON. case_json <parent dir> and
# key=value edits applied by python (a value of DEL removes the key).
case_json() {
    python3 - "$@" <<'PY'
import json, sys
parent, edits = sys.argv[1], sys.argv[2:]
import os
e = os.environ
a = {
    "parent": {"input": "ihp-open-pdk",
               "src": {"outPath": parent, "rev": e["rev_p"], "narHash": e["nar_p"]}},
    "submodules": {
        e["sub_b"]: {"input": "pypreprocessor",
                     "src": {"outPath": e["sb"], "rev": e["rev_b"], "narHash": e["nar_b"]}},
        e["sub_a"]: {"input": "pycell4klayout-api",
                     "src": {"outPath": e["sa"], "rev": e["rev_a"], "narHash": e["nar_a"]}},
    },
}
for edit in edits:
    key, _, value = edit.partition("=")
    *path, last = key.split(".")
    node = a
    for k in path:
        node = node[{"A": e["sub_a"], "B": e["sub_b"]}.get(k, k)]
    last = {"A": e["sub_a"], "B": e["sub_b"]}.get(last, last)
    if value == "DEL":
        del node[last]
    else:
        node[last] = json.loads(value) if value.startswith(("{", "[")) else value
print(json.dumps(a))
PY
}
export rev_p rev_a rev_b nar_p nar_a nar_b sub_a sub_b sa sb

# statement <parent dir> [edits...]: evaluate, JSON to $work/stmt.json.
statement() {
    nix-instantiate --eval --strict --json --argstr args "$(case_json "$@")" --expr "
      { args }:
      let r = import $here/pdk-source.nix (builtins.fromJSON args);
      in { inherit (r) file format statement json; placeholder = builtins.placeholder \"out\"; }
    " >"$work/stmt.json"
}

# ---- evaluation ------------------------------------------------------------

expect 0 "a parent with two of its five submodules copied" statement "$good_parent"
expect 0 "... states exactly the inputs, sorted, and the three left out" python3 - "$work/stmt.json" <<'PY'
import json, os, sys
e = os.environ
r = json.load(open(sys.argv[1]))
want = {
    "format": "adk-tools-pdk-source/v1",
    "parent": {"input": "ihp-open-pdk", "rev": e["rev_p"], "narHash": e["nar_p"]},
    "submodules": [
        {"path": e["sub_a"], "input": "pycell4klayout-api", "rev": e["rev_a"], "narHash": e["nar_a"]},
        {"path": e["sub_b"], "input": "pypreprocessor", "rev": e["rev_b"], "narHash": e["nar_b"]},
    ],
    "not_fetched": [
        "ihp-sg13g2/libs.tech/digital",
        "ihp-sg13g2/libs.tech/openems/openems_ihp_sg13g2",
        "ihp-sg13g2/libs.tech/palace",
    ],
}
assert r["file"] == "pdk-source.json", r["file"]
assert r["format"] == want["format"], r["format"]
assert r["statement"] == want, r["statement"]
text = json.dumps(dict(want, materialized_at=r["placeholder"]), sort_keys=True, separators=(",", ":"))
assert r["json"] == text, (r["json"], text)
assert "materialized_at" not in r["statement"]
PY

expect 0 "no submodule copied" statement "$good_parent" submodules='{}'
expect 0 "... leaves all five in not_fetched, in order" python3 -c "
import json, sys
r = json.load(open('$work/stmt.json'))['statement']
assert r['submodules'] == [], r
assert r['not_fetched'] == sorted(r['not_fetched']) and len(r['not_fetched']) == 5, r
"

odd="$(parent_tree odd "$(printf '[Submodule "x"]\r\n  Path=%s  \r\n[submodule "y"]\n\tpath\t=\t%s\n# path = a/comment\n' "$sub_a" "$sub_b")")"
expect 0 "a capitalised key, no spaces, CRLF, tabs and a comment line" statement "$odd"
expect 0 "... read as the two paths" python3 -c "
import json
r = json.load(open('$work/stmt.json'))['statement']
assert [s['path'] for s in r['submodules']] == ['$sub_a', '$sub_b'] and r['not_fetched'] == [], r
"

refuse() {
    local what="$1" pattern="$2"
    shift 2
    expect 1 "$what" statement "$@"
    expect_out "... named" "$pattern"
}
refuse "a parent without a rev (a path: input)" "the parent \\(ihp-open-pdk\\) has no 40-hex rev" \
    "$good_parent" parent.src.rev=DEL
refuse "a parent with a dirtyRev only" "the parent \\(ihp-open-pdk\\) has no 40-hex rev" \
    "$good_parent" parent.src.rev=DEL parent.src.dirtyRev="$rev_p-dirty"
refuse "a submodule without a rev" "$sub_b \\(pypreprocessor\\) has no 40-hex rev" \
    "$good_parent" submodules.B.src.rev=DEL
refuse "a short rev" "has no 40-hex rev" "$good_parent" submodules.A.src.rev=2222222
refuse "an uppercase rev" "has no 40-hex rev" "$good_parent" parent.src.rev=ABCDEF1111111111111111111111111111111111
refuse "a parent without a narHash" "the parent \\(ihp-open-pdk\\) has no sha256 narHash" \
    "$good_parent" parent.src.narHash=DEL
refuse "a sha1 narHash" "has no sha256 narHash" "$good_parent" submodules.A.src.narHash=sha1-AAAAAAAAAAAAAAAAAAAAAAAAAAA=
refuse "an empty input name" "the parent names no flake input" "$good_parent" parent.input=
refuse "a copied submodule .gitmodules does not declare" "is copied in, but the parent's .gitmodules does not declare it" \
    "$good_parent" submodules.not/declared='{"input": "x", "src": {"outPath": "/x", "rev": "'$rev_a'", "narHash": "'$nar_a'"}}'
dup="$(parent_tree dup "$(gitmodules; printf '[submodule "again"]\n\tpath = %s\n' ihp-sg13g2/libs.tech/palace)")"
refuse "a path .gitmodules declares twice" "declares ihp-sg13g2/libs.tech/palace more than once" "$dup"
n=0
for line in 'path = "quoted/path"' 'path = a/path ; comment' 'path = a/path # comment' 'path = back\slash' 'path =' 'path =   '; do
    n=$((n + 1))
    bad="$(parent_tree "bad$n" "$(gitmodules; printf '[submodule "z"]\n\t%s\n' "$line")")"
    refuse "a .gitmodules line: $line" "a .gitmodules path line this does not read" "$bad"
done
for text in '[submodule "q"] path = ihp-sg13g2/libs.tech/palace' '[include]\n\tpath = ../shared' \
    '[submodule "a\\"b"]\n\tpath = ihp-sg13g2/libs.tech/palace' '[core]'; do
    n=$((n + 1))
    bad="$(parent_tree "bad$n" "$(gitmodules; printf "$text\n")")"
    refuse "a .gitmodules section line: $text" "a .gitmodules section line this does not read" "$bad"
done
none="$(parent_tree none "")"
rm "$none/.gitmodules"
expect 1 "a parent without .gitmodules, nothing copied" statement "$none" submodules='{}'
expect_out "... named" "opening file '$none/.gitmodules'"

# ---- build -----------------------------------------------------------------

if [ "$eval_only" -eq 1 ]; then
    echo "pdk-source: build part not run (--eval-only)"
    echo "pdk-source: $passed passed, $failed failed"
    [ "$failed" -eq 0 ]
    exit
fi

# nixpkgs as flake.lock names it, from the store only: its path is computed
# from the locked narHash and must be valid before anything imports it. An
# evaluation that fetched it would do so before a dry run could see it.
nixpkgs_hash="$(python3 -c "
import json
print(json.load(open('$lock'))['nodes']['nixpkgs']['locked']['narHash'])
")"
nixpkgs="$(nix-store --print-fixed-path --recursive sha256 \
    "$(nix hash convert --hash-algo sha256 --to nix32 "$nixpkgs_hash")" source)" || {
    echo "FAIL: no store path computed for the locked nixpkgs $nixpkgs_hash"
    exit 1
}
if ! nix-store --check-validity "$nixpkgs" 2>/dev/null; then
    echo "FAIL: the locked nixpkgs ($nixpkgs) is not in the store; not fetching it"
    exit 1
fi
build_expr() {
    cat <<NIX
{ parent, args }:
let
  pkgs = import (builtins.storePath "$nixpkgs") {
    system = builtins.currentSystem; config = { }; overlays = [ ];
  };
  a = builtins.fromJSON args;
  store = name: src: src // { outPath = builtins.path { path = src.outPath; inherit name; }; };
in
pkgs.callPackage $here/sg13g2-pdk.nix {
  parent = a.parent // { src = store "stub-parent" (a.parent.src // { outPath = parent; }); };
  submodules = builtins.mapAttrs (p: s: s // { src = store "stub-sub" s.src; }) a.submodules;
}
NIX
}
# build <parent dir> [edits...]: out path on stdout, or a failed build.
build() {
    local p="$1"
    shift
    build_expr >"$work/build.nix"
    nix-build --no-out-link --option substitute false --max-jobs 1 --cores 1 \
        --argstr parent "$p" --argstr args "$(case_json /unused "$@")" "$work/build.nix"
}

# Refuse to go on when anything but the stub trees would be built or fetched:
# a nixpkgs or stdenv that is not in the store would mean a bootstrap.
build_expr >"$work/build.nix"
if ! nix-build --dry-run --option substitute false --argstr parent "$good_parent" \
    --argstr args "$(case_json /unused)" "$work/build.nix" >"$work/dry.out" 2>&1; then
    echo "FAIL: the dry run of the stub build failed"
    sed 's/^/    /' "$work/dry.out"
    exit 1
fi
if grep -q "will be fetched" "$work/dry.out" \
    || grep -E '^ +/nix/store/' "$work/dry.out" | grep -v -q -E -- '-ihp-open-pdk-1111111\.drv$'; then
    echo "FAIL: the stub build would fetch or build more than its own tree; not running it"
    sed 's/^/    /' "$work/dry.out"
    exit 1
fi

check_tree() {
    python3 - "$1" <<'PY'
import json, os, stat, sys
e = os.environ
out = sys.argv[1]
f = os.path.join(out, "pdk-source.json")
st = os.lstat(f)
assert stat.S_ISREG(st.st_mode), "not a regular file"
assert not st.st_mode & 0o222, oct(st.st_mode)
raw = open(f, "rb").read()
assert raw.endswith(b"}\n") and not raw.endswith(b"\n\n"), raw[-3:]
s = json.loads(raw)
assert s["materialized_at"] == os.path.realpath(out) == out, (s["materialized_at"], out)
assert raw == (json.dumps(s, sort_keys=True, separators=(",", ":")) + "\n").encode()
assert s["format"] == "adk-tools-pdk-source/v1"
assert [m["path"] for m in s["submodules"]] == [e["sub_a"], e["sub_b"]]
assert len(s["not_fetched"]) == 3
for m in s["submodules"]:
    assert open(os.path.join(out, m["path"], "marker")).read().strip() in ("sub-a", "sub-b")
assert open(os.path.join(out, "ihp-sg13g2", "README")).read() == "parent file\n"
PY
}

expect 0 "the stub tree builds" build "$good_parent"
out="$(tail -n 1 "$work/last.out")"
expect 0 "... its pdk-source.json: read-only, one line, materialized_at its own path" check_tree "$out"
expect 0 "... conforms to formats/adk-tools-pdk-source.v1.schema.json" \
    python3 "$here/../tests/formats/check_format.py" \
    "$here/../formats/adk-tools-pdk-source.v1.schema.json" "$out/pdk-source.json"
expect 1 "... and carries no adkIdentity: the context record reads the statement" \
    nix-instantiate --eval --strict --json --option substitute false \
    --argstr parent "$good_parent" --argstr args "$(case_json /unused)" "$work/build.nix" \
    -A adkIdentity
expect_out "... named" "attribute 'adkIdentity' in selection path 'adkIdentity' not found"

stray="$(parent_tree stray)"
echo '{}' >"$stray/pdk-source.json"
expect 1 "a parent that already has a pdk-source.json" build "$stray"
expect_out "... named" "pdk-source.json: already in the parent tree"
link="$(parent_tree link)"
ln -s nowhere "$link/pdk-source.json"
expect 1 "a parent with a dangling pdk-source.json symlink" build "$link"
expect_out "... named" "pdk-source.json: already in the parent tree"
full="$(parent_tree full)"
mkdir -p "$full/$sub_a" && echo x >"$full/$sub_a/f"
expect 1 "a submodule path not empty in the parent" build "$full"
expect_out "... named" "$sub_a: not empty in the parent tree"

echo "pdk-source: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
