#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# The build variant's pins check (docs/E2E_BUILD_VARIANT.md). The flake reads
# LIBRELANE_REV and LIBRELANE_PATCHES from staging.pin and SG13G2_REF from the
# Dockerfile, and its evaluation refuses a lock that disagrees on the LibreLane
# and parent PDK revisions. What evaluation cannot see needs git or a build,
# and is checked here:
#
#   lock       flake.lock's librelane, ihp-open-pdk, pycell4klayout-api and
#              pypreprocessor revisions against staging.pin, the Dockerfile and
#              the parent's gitlinks at SG13G2_REF, and each submodule's
#              owner/repo against the parent's .gitmodules. No Nix; read from
#              the lock and a local IHP-Open-PDK clone.
#   openroad   by evaluation alone, as nix/check-openroad-pin.sh evaluates: the
#              LibreLane package's derivation graph builds staging.pin's
#              NIX_OPENROAD_OUTPATH and no other derivation of the same name.
#   patched    with --realize only, because it builds one derivation (the
#              patched source, which applies a patch and copies a tree): the
#              realised packages.librelane-src against the tree `git apply`
#              makes from LIBRELANE_REV and LIBRELANE_PATCHES, in order, in a
#              local LibreLane clone, file by file. Submodule paths are left
#              out on both sides; the flake input carries none.
#
# LIBRELANE_PATCHED_REV names the commit an earlier recipe made from those
# two with fixed identity and dates. Its message is not among staging.pin's
# constants, so it cannot be remade from the pin; when the clone holds that
# commit its tree is compared too. Otherwise it stays UNVERIFIED: the patched
# source being right says nothing about that historical commit, and the last
# line, which names every item checked and every item not, says so.
#
# Usage: nix/check-build-pins.sh --pdk-git <IHP-Open-PDK clone>
#            [--realize --librelane-git <LibreLane clone>] [<adk-tools checkout>]
# Exit 0 only when every check that ran passed; the last line is then
# "pins: checked ...", followed by "; UNVERIFIED ..." for what did not run.

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/cli-env.sh"

pdk_git=""
librelane_git=""
realize=""
at=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --pdk-git) pdk_git="${2:-}"; shift 2 || adk_nix_die "pins: --pdk-git needs a directory" ;;
        --librelane-git) librelane_git="${2:-}"; shift 2 || adk_nix_die "pins: --librelane-git needs a directory" ;;
        --realize) realize=1; shift ;;
        -*) adk_nix_die "pins: unknown option $1" ;;
        *) [ -z "$at" ] || adk_nix_die "pins: one checkout only"; at="$1"; shift ;;
    esac
done
at="${at:-$(cd "$here/.." && pwd)}"
[ -n "$pdk_git" ] || adk_nix_die "pins: --pdk-git is required (the gitlinks are read there)"
[ -z "$realize" ] || [ -n "$librelane_git" ] || adk_nix_die "pins: --realize needs --librelane-git"
command -v python3 >/dev/null || adk_nix_die "pins: python3 is needed to read flake.lock"
system="${ADK_NIX_SYSTEM:-x86_64-linux}"
# For nix/test-check-build-pins.sh, which runs this against a stand-in nix.
store="${ADK_NIX_STORE_DIR:-/nix/store}"

# One literal assignment in a file, as written: `<prefix><40 hex>`, exactly
# one line. For staging.pin the value is also taken as a shell reads it, and
# the two must agree, as check-openroad-pin.sh requires.
one_rev() {
    local file="$1" prefix="$2" count value
    count="$(grep -c -E "^${prefix}[0-9a-f]{40}\$" "$file")"
    [ "$count" -eq 1 ] || adk_nix_die "pins: $count lines ${prefix}<rev> in $file, expected 1"
    value="$(sed -n -E "s/^${prefix}([0-9a-f]{40})\$/\\1/p" "$file")"
    echo "$value"
}

pin="$at/staging.pin"
[ -f "$pin" ] && [ -f "$at/Dockerfile" ] && [ -f "$at/flake.lock" ] || \
    adk_nix_die "pins: $at has no staging.pin, Dockerfile or flake.lock"
librelane_rev="$(one_rev "$pin" "LIBRELANE_REV=")" || exit 1
patched_rev="$(one_rev "$pin" "LIBRELANE_PATCHED_REV=")" || exit 1
for var in LIBRELANE_REV LIBRELANE_PATCHED_REV; do
    want="$(adk_nix_pin "$pin" "$var")" || adk_nix_die "pins: $pin does not source cleanly"
    case "$var" in
        LIBRELANE_REV) [ "$want" = "$librelane_rev" ] ;;
        *) [ "$want" = "$patched_rev" ] ;;
    esac || adk_nix_die "pins: $pin sets $var to something other than its literal"
done
patches="$(adk_nix_pin "$pin" LIBRELANE_PATCHES)" || adk_nix_die "pins: $pin does not source cleanly"
sg13g2_rev="$(one_rev "$at/Dockerfile" "ARG SG13G2_REF=")" || exit 1

# --- lock ------------------------------------------------------------------

# "<rev> <owner>/<repo>" of a root input in flake.lock, or nothing.
locked() {
    python3 - "$at/flake.lock" "$1" <<'EOF'
import json, sys
lock = json.load(open(sys.argv[1]))
nodes = lock["nodes"]
name = nodes[lock["root"]]["inputs"].get(sys.argv[2])
node = nodes.get(name) if isinstance(name, str) else None
got = (node or {}).get("locked", {})
if got.get("type") == "github" and got.get("rev"):
    print(got["rev"], "%s/%s" % (got.get("owner", ""), got.get("repo", "")))
EOF
}

# The GitHub owner/repo a submodule path's URL names in .gitmodules at a
# commit, lower-cased, or nothing.
gitmodule_repo() {
    adk_nix_git -C "$pdk_git" config --blob "$1:.gitmodules" --get-regexp '^submodule\..*\.path$' |
    while read -r key path; do
        [ "$path" = "$2" ] || continue
        adk_nix_git -C "$pdk_git" config --blob "$1:.gitmodules" --get "${key%.path}.url"
    done | sed -E 's#^(https://|git@)github\.com[:/]##; s#\.git$##; s#/$##' | tr 'A-Z' 'a-z'
}

adk_nix_git -C "$pdk_git" cat-file -e "$sg13g2_rev^{commit}" 2>/dev/null || \
    adk_nix_die "pins: $pdk_git does not hold SG13G2_REF $sg13g2_rev"

read -r got repo <<< "$(locked librelane)"
[ "${got:-}" = "$librelane_rev" ] || \
    adk_nix_die "pins: flake.lock's librelane is ${got:-missing}, staging.pin's LIBRELANE_REV is $librelane_rev"
read -r got repo <<< "$(locked ihp-open-pdk)"
[ "${got:-}" = "$sg13g2_rev" ] || \
    adk_nix_die "pins: flake.lock's ihp-open-pdk is ${got:-missing}, the Dockerfile's SG13G2_REF is $sg13g2_rev"
[ "${repo,,}" = ihp-gmbh/ihp-open-pdk ] || adk_nix_die "pins: flake.lock's ihp-open-pdk is from $repo"
for sub in pycell4klayout-api pypreprocessor; do
    path="ihp-sg13g2/libs.tech/klayout/python/$sub"
    link="$(adk_nix_git -C "$pdk_git" ls-tree "$sg13g2_rev" -- "$path" | awk '$2 == "commit" { print $3 }')"
    [[ "$link" =~ ^[0-9a-f]{40}$ ]] || adk_nix_die "pins: no gitlink at $path in SG13G2_REF $sg13g2_rev"
    read -r got repo <<< "$(locked "$sub")"
    [ "${got:-}" = "$link" ] || \
        adk_nix_die "pins: flake.lock's $sub is ${got:-missing}, SG13G2_REF's gitlink at $path is $link"
    want_repo="$(gitmodule_repo "$sg13g2_rev" "$path")"
    [ -n "$want_repo" ] && [ "${repo,,}" = "$want_repo" ] || \
        adk_nix_die "pins: flake.lock's $sub is from $repo, SG13G2_REF's .gitmodules names ${want_repo:-nothing}"
    adk_nix_note "pins: $sub $link is SG13G2_REF's gitlink, from $repo"
done
adk_nix_note "pins: lock agrees with staging.pin, the Dockerfile and the parent's gitlinks"

# --- openroad --------------------------------------------------------------

openroad="$(adk_nix_pin "$pin" NIX_OPENROAD_OUTPATH)" || adk_nix_die "pins: $pin does not source cleanly"
[[ "${openroad#"$store"/}" =~ ^[0-9a-z]{32}-openroad[^/]*$ ]] && [ "${openroad#"$store"/}" != "$openroad" ] || \
    adk_nix_die "pins: NIX_OPENROAD_OUTPATH in $pin is not a $store/<hash>-openroad path"
graph="$(mktemp "${TMPDIR:-/tmp}/adk-pins-graph.XXXXXX")" || adk_nix_die "pins: mktemp failed"
trap 'rm -f "$graph"' EXIT
nix derivation show --recursive --max-jobs 0 --no-update-lock-file \
    --option allow-import-from-derivation false \
    "$at#packages.$system.librelane" > "$graph" || \
    adk_nix_die "pins: nix derivation show of $at#packages.$system.librelane failed"
python3 - "$graph" "$openroad" <<'EOF' || exit 1
import json, sys
graph = json.load(open(sys.argv[1]))
if isinstance(graph, dict) and "derivations" in graph:
    graph = graph["derivations"]
want = sys.argv[2]
names = {}
for drv, d in graph.items():
    for out in d.get("outputs", {}).values():
        if out.get("path"):
            names.setdefault(d.get("name") or d.get("env", {}).get("name"), set()).add(out["path"])
pinned = [n for n, paths in names.items() if want in paths]
if len(pinned) != 1:
    sys.exit("adk-nix: pins: LibreLane's derivation graph does not build %s" % want)
others = sorted(names[pinned[0]] - {want})
if others:
    sys.exit("adk-nix: pins: LibreLane's graph builds another %s: %s" % (pinned[0], " ".join(others)))
print("adk-nix: pins: LibreLane's graph builds OpenROAD %s and no other %s" % (want, pinned[0]),
      file=sys.stderr)
EOF

# --- patched ---------------------------------------------------------------

if [ -z "$realize" ]; then
    adk_nix_note "pins: checked lock, openroad; UNVERIFIED patched source, LIBRELANE_PATCHED_REV $patched_rev (no --realize)"
    exit 0
fi
adk_nix_git -C "$librelane_git" cat-file -e "$librelane_rev^{commit}" 2>/dev/null || \
    adk_nix_die "pins: $librelane_git does not hold LIBRELANE_REV $librelane_rev"
src="$(nix build --no-link --print-out-paths --max-jobs 1 --cores 2 --no-update-lock-file \
          --option allow-import-from-derivation false "$at#packages.$system.librelane-src")" || \
    adk_nix_die "pins: building $at#packages.$system.librelane-src failed"
[[ "${src#"$store"/}" =~ ^[0-9a-z]{32}-[^/[:space:]]+$ ]] && [ "${src#"$store"/}" != "$src" ] && [ -d "$src" ] || \
    adk_nix_die "pins: the build printed $src, not one store directory"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-pins.XXXXXX")" || adk_nix_die "pins: mktemp failed"
trap 'rm -f "$graph"; rm -rf "$work"' EXIT
tree="$work/librelane"
adk_nix_git clone -q --shared --no-checkout "$librelane_git" "$tree" && \
    adk_nix_git -C "$tree" checkout -q --detach "$librelane_rev" || \
    adk_nix_die "pins: cannot check out LIBRELANE_REV from $librelane_git"
for p in $patches; do
    [ -f "$at/$p" ] || adk_nix_die "pins: LIBRELANE_PATCHES names $p, not a file in $at"
    adk_nix_git -C "$tree" apply --whitespace=nowarn "$at/$p" || adk_nix_die "pins: $p does not apply to LIBRELANE_REV"
done
adk_nix_git -C "$tree" add -A && expected_tree="$(adk_nix_git -C "$tree" write-tree)" || \
    adk_nix_die "pins: cannot record the patched tree"

# Every regular file and symlink on each side, with its kind, executable bit
# and content digest; submodule paths (gitlinks at LIBRELANE_REV) left out.
listing() {
    python3 - "$1" "$2" <<'EOF'
import hashlib, os, stat, sys
root, skip = sys.argv[1], set(filter(None, sys.argv[2].split("\n")))
out = []
for dirpath, dirs, files in os.walk(root):
    rel = os.path.relpath(dirpath, root)
    dirs[:] = sorted(d for d in dirs
                     if not (rel == "." and d == ".git")
                     and os.path.normpath(os.path.join(rel, d)) not in skip)
    for name in files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
        path = os.path.join(dirpath, name)
        st = os.lstat(path)
        key = os.path.normpath(os.path.join(rel, name))
        if stat.S_ISLNK(st.st_mode):
            out.append("link %s %s" % (hashlib.sha256(os.readlink(path).encode()).hexdigest(), key))
        elif stat.S_ISREG(st.st_mode):
            kind = "exec" if st.st_mode & 0o111 else "file"
            out.append("%s %s %s" % (kind, hashlib.sha256(open(path, "rb").read()).hexdigest(), key))
        else:
            out.append("other - %s" % key)
for line in sorted(set(out)):
    print(line)
EOF
}
skip="$(adk_nix_git -C "$tree" ls-tree -r "$librelane_rev" | awk '$2 == "commit" { print $4 }')"
listing "$tree" "$skip" > "$work/expected" && listing "$src" "$skip" > "$work/realised" || \
    adk_nix_die "pins: cannot list the two trees"
if ! cmp -s "$work/expected" "$work/realised"; then
    diff "$work/expected" "$work/realised" | head -n 20 >&2
    adk_nix_die "pins: $src is not LIBRELANE_REV with LIBRELANE_PATCHES applied (< expected, > realised)"
fi
adk_nix_note "pins: $src is LIBRELANE_REV with LIBRELANE_PATCHES applied, file by file"
if adk_nix_git -C "$librelane_git" cat-file -e "$patched_rev^{commit}" 2>/dev/null; then
    [ "$(adk_nix_git -C "$librelane_git" rev-parse "$patched_rev^{tree}")" = "$expected_tree" ] || \
        adk_nix_die "pins: LIBRELANE_PATCHED_REV's tree is not LIBRELANE_REV with the patches applied"
    adk_nix_note "pins: LIBRELANE_PATCHED_REV $patched_rev has that tree"
    adk_nix_note "pins: checked lock, openroad, patched source, LIBRELANE_PATCHED_REV $patched_rev"
else
    adk_nix_note "pins: checked lock, openroad, patched source; UNVERIFIED LIBRELANE_PATCHED_REV $patched_rev (not in $librelane_git, a historical commit this check cannot remake)"
fi
