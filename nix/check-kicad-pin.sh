#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Checks flake.lock's kicad-fork input against the tools/kicad submodule, which
# stays the owner of the KiCad fork's revision. Git and the lock only: no Nix,
# no network. Read from the checkout: the gitlink at tools/kicad in HEAD, the
# .gitmodules HEAD holds, and the working tree's flake.lock (the one an
# evaluation reads). Exit 0 only when the locked input is a GitHub input at
# the gitlink's commit, from the repository .gitmodules names for tools/kicad,
# with a narHash. Each of these refuses, and says which: no gitlink at
# tools/kicad, .gitmodules naming no submodule or more than one at that path,
# a URL that is not a GitHub repository, no kicad-fork root input, an input
# that is not type github, and a rev, a repository or a narHash that differs
# or is missing. Owner and repository compare case-insensitively, as GitHub
# resolves them, with the URL's `.git` suffix and trailing slash dropped.
#
# Usage: nix/check-kicad-pin.sh [<adk-tools checkout, default this one>]

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/cli-env.sh"

at="${1:-$(cd "$here/.." && pwd)}"
path=tools/kicad
input=kicad-fork
lock="$at/flake.lock"
[ -f "$lock" ] || adk_nix_die "kicad pin: $lock not found"
command -v python3 >/dev/null || adk_nix_die "kicad pin: python3 is needed to read flake.lock"
adk_nix_git -C "$at" rev-parse --verify --quiet HEAD >/dev/null || \
    adk_nix_die "kicad pin: $at is not a git checkout with a HEAD"

link="$(adk_nix_gitlink "$at" "$path")"
[[ "$link" =~ ^[0-9a-f]{40}$ ]] || adk_nix_die "kicad pin: no gitlink at $path in $at's HEAD"

# The URL of every .gitmodules entry whose path is tools/kicad, one per line.
urls="$(adk_nix_git -C "$at" config --blob HEAD:.gitmodules --get-regexp '^submodule\..*\.path$' 2>/dev/null |
    while read -r key value; do
        [ "$value" = "$path" ] || continue
        adk_nix_git -C "$at" config --blob HEAD:.gitmodules --get "${key%.path}.url" || echo "-"
    done)"
count="$(printf '%s' "$urls" | grep -c .)"
[ "$count" -eq 1 ] || \
    adk_nix_die "kicad pin: HEAD's .gitmodules has $count submodules at $path, expected 1"
repo="${urls%/}"
repo="${repo%.git}"
repo="$(printf '%s\n' "$repo" | sed -n -E 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)##p')"
[[ "$repo" =~ ^[-._A-Za-z0-9]+/[-._A-Za-z0-9]+$ ]] || \
    adk_nix_die "kicad pin: .gitmodules' URL for $path is not a GitHub repository: $urls"

python3 - "$lock" "$input" "$link" "$repo" <<'EOF' || exit 1
import json, sys
path, name, link, repo = sys.argv[1:5]
def die(msg):
    sys.exit("adk-nix: kicad pin: " + msg)
try:
    lock = json.load(open(path))
    nodes = lock["nodes"]
    root = nodes[lock["root"]]["inputs"]
except (OSError, ValueError, KeyError, TypeError) as e:
    die("cannot read %s: %s" % (path, e))
ref = root.get(name)
node = nodes.get(ref) if isinstance(ref, str) else None
if not isinstance(node, dict):
    die("flake.lock has no %s root input" % name)
got = node.get("locked") or {}
if got.get("type") != "github":
    die("flake.lock's %s is type %s, expected github" % (name, got.get("type")))
if got.get("rev") != link:
    die("flake.lock's %s is %s, the tools/kicad gitlink is %s" % (name, got.get("rev"), link))
locked_repo = "%s/%s" % (got.get("owner", ""), got.get("repo", ""))
if locked_repo.lower() != repo.lower():
    die("flake.lock's %s is from %s, .gitmodules names %s for tools/kicad" % (name, locked_repo, repo))
if not str(got.get("narHash", "")).startswith("sha256-"):
    die("flake.lock's %s has no narHash" % name)
EOF
adk_nix_note "kicad pin: flake.lock's $input is $link from $repo, the tools/kicad gitlink and its .gitmodules repository"
