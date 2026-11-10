#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Checks devShells.tail's wrappers against the image's, from git alone: no Nix,
# no network. Everything is read from HEAD of the checkout, so an edit that is
# not committed is not what this passes. Exit 0 only when:
#
#   - each of the image's tail commands (chiplet-export, adk-verify-carrier-drc,
#     adk-drc) is bin/<name> and has a template nix-tail/<name>.in;
#   - each template is printable ASCII (tabs and newlines aside), so it splits
#     into the lines bash sees;
#   - each template is `#!@bash@ -p`, then only comments, the
#     `: "${VAR:?...}"` guards its command needs (SHAPE: exactly those),
#     `export ADK_PYTHON="$ADK_NIX_VENV/bin/python"` where the image's script
#     starts a second Python (and nowhere else), and last one exec line of
#     exactly `exec <interpreter> <script> "$@"`, in that order: the arguments
#     go on whole and nothing is put before them;
#   - each exec line's interpreter is the one the tail gives that command:
#     tail-export-python for chiplet-export, the venv's for the other two;
#   - every path an exec line names is a placeholder the flake fills with a
#     store path, or lies under ADK_NIX_VENV (the CLI shell's venv, whose
#     interpreter links into the store), ADK_ROOT or ADK_NIX_PLUGIN_ROOT, the
#     last two being tools/adk and tools/chiplet_kicad_plugin in evidence mode
#     (read from nix/cli-env.sh's ADK_NIX_ROOTS and nix-tail/tail-env.sh);
#   - the script each exec line runs is the image's: chiplet-export and
#     adk-drc run the same file below their root as bin/<name> does, and
#     pass "$@" as their last word; adk-verify-carrier-drc runs the store copy
#     of bin/adk-verify-carrier-drc;
#   - flake.nix has one line that reads like a binding `tail-wrappers = ...`
#     (a line that starts with # is not counted unless it holds '', ", */ or
#     a carriage return, any of which could end a string or a comment before
#     a binding on that line), and from there the binding is printable ASCII
#     and, line for line with only leading spaces and tabs aside, the one this
#     check holds (BINDING): no comment, one substitute call per name that
#     fills exactly the placeholders the templates use, each spelled as FILL
#     spells it (bash, exportPython, carrierDrc), for exactly these three
#     names.
#
# The flake is read as text, in that one form of binding only. This check
# does not see tail-wrappers bound in another form (quoted, split over lines,
# inherited from elsewhere, overridden), a copy of the binding Nix never
# evaluates (in a /* */ comment or an unused let), or what the names the
# binding uses evaluate to (lib, pkgs and FILL's values).
# nix-tail/check-tail-eval.sh reads the tail-wrappers derivation Nix
# evaluates for the tail shell, in whatever form it is bound, and refuses one
# whose build command (store paths aside), variables or wrappers' commands
# are not what this binding gives; it checks the bash that fills @bash@ by
# shape only, so a bash that drops -p is caught by neither check.
#
# Usage: nix-tail/check-tail-wrappers.sh [<adk-tools checkout, default this one>]

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
ADK_NIX_LIB_ONLY=1 . "$here/../nix/cli-env.sh"

at="${1:-$(cd "$here/.." && pwd)}"
command -v python3 >/dev/null || adk_nix_die "tail wrappers: python3 is needed"
adk_nix_git -C "$at" rev-parse --verify --quiet HEAD >/dev/null || \
    adk_nix_die "tail wrappers: $at is not a git checkout with a HEAD"

work="$(mktemp -d "${TMPDIR:-/tmp}/adk-tail-wrappers.XXXXXX")" || adk_nix_die "mktemp failed"
trap 'rm -rf "$work"' EXIT
names="chiplet-export adk-verify-carrier-drc adk-drc"
files="flake.nix nix/cli-env.sh nix-tail/tail-env.sh"
for name in $names; do
    files="$files bin/$name nix-tail/$name.in"
done
for f in $files; do
    mkdir -p "$work/$(dirname "$f")"
    adk_nix_git -C "$at" show "HEAD:$f" > "$work/$f" 2>/dev/null || \
        adk_nix_die "tail wrappers: $f is not in HEAD of $at"
done

python3 - "$work" $names <<'EOF' || exit 1
import os, re, sys

work, names = sys.argv[1], sys.argv[2:]
problems = []


def read(path):
    # newline="": a carriage return stays in the text, where bash sees it.
    with open(os.path.join(work, path), encoding="utf-8", errors="surrogateescape", newline="") as handle:
        return handle.read()


# Where each variable a wrapper may use is rooted in evidence mode.
roots = {"ADK_NIX_VENV": None}
m = re.search(r"^ADK_ROOT\|(tools/[^\s|]+)$", read("nix/cli-env.sh"), re.M)
roots["ADK_ROOT"] = m.group(1) if m else None
m = re.search(r"^ADK_NIX_TAIL_PLUGIN_PATH=(tools/\S+)$", read("nix-tail/tail-env.sh"), re.M)
roots["ADK_NIX_PLUGIN_ROOT"] = m.group(1) if m else None
if roots["ADK_ROOT"] != "tools/adk":
    problems.append(f"nix/cli-env.sh roots ADK_ROOT at {roots['ADK_ROOT']}, not tools/adk")
if roots["ADK_NIX_PLUGIN_ROOT"] != "tools/chiplet_kicad_plugin":
    problems.append(f"nix-tail/tail-env.sh roots the plugin at {roots['ADK_NIX_PLUGIN_ROOT']}, "
                    f"not tools/chiplet_kicad_plugin")

WORD = r"""(?:'@(?P<{0}p>[A-Za-z]+)@(?P<{0}ps>/[A-Za-z0-9_./-]*)?'""" \
       r"""|"\$(?P<{0}v>[A-Z_]+)(?P<{0}vs>/[A-Za-z0-9_./-]+)")"""
EXEC = re.compile(r'exec ' + WORD.format("i") + " " + WORD.format("s") + r' "\$@"')
GUARD = re.compile(r': "\$\{(?P<var>[A-Z_]+):\?enter through devShells\.tail\}"')
EXPORT = 'export ADK_PYTHON="$ADK_NIX_VENV/bin/python"'
# The file each command's script is below its root, as the image's bin/<name>
# names it; None: the command is the image's file itself.
IMAGE_SCRIPT = {"chiplet-export": ("ADK_NIX_PLUGIN_ROOT", "/plugins/chiplet_export/chiplet_export_cli.py"),
                "adk-drc": ("ADK_ROOT", "/klayout/drc/run_drc.py"),
                "adk-verify-carrier-drc": None}
VENV_PYTHON = ("$", "ADK_NIX_VENV", "/bin/python")


def shown(word):
    return f"@{word[1]}@{word[2]}" if word[0] == "@" else f"${word[1]}{word[2]}"


# Per command: the variables it guards, whether it exports ADK_PYTHON, and
# the interpreter its exec line runs.
SHAPE = {"chiplet-export": ({"ADK_NIX_PLUGIN_ROOT", "PDK_ROOT", "KICAD_CHIPLET_PYTHON",
                             "GDS_TO_KICAD_ROOT"}, False, ("@", "exportPython", "/bin/python")),
         "adk-drc": ({"ADK_NIX_VENV", "ADK_ROOT"}, False, VENV_PYTHON),
         "adk-verify-carrier-drc": ({"ADK_NIX_VENV"}, True, VENV_PYTHON)}
used = set()

for name in names:
    what = f"nix-tail/{name}.in"
    # Printable ASCII, tab and newline only: Python splits lines at characters
    # bash does not (a carriage return, a form feed, NEL), and a line this
    # check reads as a comment could hide a command bash runs.
    text = read(what)
    odd = sorted({c for c in text if c not in "\t\n" and not " " <= c <= "~"})
    if odd:
        problems.append(f"{what}: holds {odd!r}, which bash and this check would split differently")
        continue
    lines = text.split("\n")
    if not lines or lines[0] != "#!@bash@ -p":
        problems.append(f"{what}: the first line is {lines[0] if lines else None!r}, not '#!@bash@ -p'")
    else:
        used.add("bash")
    body = [l for l in lines[1:] if l.strip(" \t") and not l.lstrip(" \t").startswith("#")]
    execs = [l for l in body if l.startswith("exec ")]
    if len(execs) != 1 or body[-1:] != execs:
        problems.append(f"{what}: {len(execs)} exec lines, and the last command is not one")
        continue
    guards, exported, rest = [], False, []
    for line in body[:-1]:
        g = GUARD.fullmatch(line)
        if g and not exported and not rest:
            guards.append(g.group("var"))
        elif line == EXPORT and not exported and not rest:
            exported = True
        else:
            rest.append(line)
    for line in rest:
        problems.append(f"{what}: a line that is not a guard, the ADK_PYTHON export or the exec, "
                        f"in that order: {line!r}")
    if name not in SHAPE:
        problems.append(f"{what}: no shape is known for {name}")
        continue
    want_guards, want_export, want_interpreter = SHAPE[name]
    if sorted(guards) != sorted(want_guards):
        problems.append(f"{what}: guards {sorted(guards)}, not {sorted(want_guards)}")
    if exported != want_export:
        problems.append(f"{what}: {'exports' if exported else 'does not export'} ADK_PYTHON, "
                        f"where the image's script {'does not start' if exported else 'starts'} a second Python")
    m = EXEC.fullmatch(execs[0])
    if not m:
        problems.append(f"{what}: the exec line is not `exec <interpreter> <script> \"$@\"`: {execs[0]!r}")
        continue
    words = {}
    for k in ("i", "s"):
        if m.group(k + "p"):
            used.add(m.group(k + "p"))
            words[k] = ("@", m.group(k + "p"), m.group(k + "ps") or "")
        else:
            var = m.group(k + "v")
            if var not in roots:
                problems.append(f"{what}: names ${var}, which is not ADK_NIX_VENV, ADK_ROOT "
                                f"or ADK_NIX_PLUGIN_ROOT")
            words[k] = ("$", var, m.group(k + "vs"))
    if words["i"] != want_interpreter:
        problems.append(f"{what}: runs under {shown(words['i'])}, not {shown(want_interpreter)}")
    if any("/../" in w[2] + "/" or "//" in w[2] for w in words.values()):
        problems.append(f"{what}: a path in the exec line climbs or doubles a slash")
    image = read(f"bin/{name}")
    want = IMAGE_SCRIPT.get(name)
    if want is None:
        if words["s"][:2] != ("@", "carrierDrc") or words["s"][2]:
            problems.append(f"{what}: does not run the store copy of bin/{name} ('@carrierDrc@')")
    else:
        if words["s"][:2] != ("$", want[0]) or words["s"][2] != want[1]:
            problems.append(f"{what}: runs {words['s']}, not ${want[0]}{want[1]}")
        if not re.search(re.escape(want[1]) + r'"\s+"\$@"\s*$', image):
            problems.append(f"bin/{name}: does not end by running ...{want[1]} with \"$@\"")

# How flake.nix spells what fills each placeholder, and its tail-wrappers
# binding line by line, indentation aside.
FILL = {"bash": "${pkgs.bash}/bin/bash", "exportPython": "${tail-export-python}",
        "carrierDrc": "${./bin/adk-verify-carrier-drc}"}
SUBSTITUTE = 'substitute ${./nix-tail + "/${name}.in"} "$out/bin/${name}"'
BINDING = [
    'tail-wrappers = pkgs.runCommandLocal "adk-tail-wrappers" { } \'\'',
    'mkdir -p "$out/bin"',
    '${lib.concatMapStrings',
    "(name: ''",
    SUBSTITUTE + " \\",
    "--subst-var-by bash " + FILL["bash"] + " \\",
    "--subst-var-by exportPython " + FILL["exportPython"] + " \\",
    "--subst-var-by carrierDrc " + FILL["carrierDrc"],
    "'')",
    "[",
] + [f'"{name}"' for name in names] + [
    "]",
    "}",
    "if grep -n '@[A-Za-z][A-Za-z]*@' \"$out\"/bin/*; then",
    'echo "a placeholder is left in a wrapper" >&2',
    "exit 1",
    "fi",
    'chmod 0555 "$out"/bin/*',
    "'';",
]

before = len(problems)
lines = read("flake.nix").split("\n")


def comment(line):
    # A line that starts with # is a comment to its end, or string content
    # that goes on past it, unless it can end a string or a comment first:
    # '' or " (a string's close), */ (a block comment's) or a carriage return
    # (where a # comment ends). Such a line may hold a binding Nix reads.
    text = line.lstrip(" \t")
    return text.startswith("#") and not any(end in text for end in ("''", '"', "*/", "\r"))


heads = [i for i, line in enumerate(lines) if not comment(line)
         and re.search(r"(?<![\w.-])tail-wrappers\s*=", line)]
block = []
if len(heads) != 1:
    problems.append(f"flake.nix: {len(heads)} lines read like a tail-wrappers binding in the form "
                    f"`tail-wrappers =`; this check reads exactly one")
else:
    end = next((j for j in range(heads[0] + 1, len(lines)) if lines[j].lstrip(" \t") == "'';"), len(lines) - 1)
    # Only leading spaces and tabs are indentation. Anything after the text
    # (a space after a line's closing backslash ends bash's line there) and
    # any other character Python would strip (a vertical tab, a form feed,
    # NEL) is one bash may see.
    block = [line.lstrip(" \t") for line in lines[heads[0]:end + 1]]
    odd = sorted({c for line in block for c in line if c != "\t" and not " " <= c <= "~"})
    if odd:
        problems.append(f"flake.nix: the tail-wrappers binding holds {odd!r}; it takes printable ASCII only")
# A # comment is named; any other departure, a /* */ comment included, is
# the line BINDING reports last.
comments = [line for line in block if "#" in line]
if comments:
    problems.append(f"flake.nix: the tail-wrappers binding holds a comment ({comments[0]!r}), "
                    f"which could repeat what this check reads; it takes none")
text = "\n".join(block)
starts = [i for i, line in enumerate(block) if line.startswith("substitute ")]
calls = len(re.findall(r"\bsubstitute", text))
filled = None
if block and (len(starts) != 1 or calls != 1):
    problems.append(f"flake.nix: tail-wrappers has {calls} substitute calls, not one")
elif block:
    call, i = [], starts[0]
    while i < len(block):
        call.append(block[i].removesuffix("\\").strip(" \t"))
        if not block[i].endswith("\\"):
            break
        i += 1
    call = " ".join(call)
    m = re.fullmatch(re.escape(SUBSTITUTE) + r"((?: --subst-var-by \S+ \S+)*)", call)
    if not m:
        problems.append(f"flake.nix: the substitute call is not `{SUBSTITUTE}` and --subst-var-by "
                        f"pairs: {call!r}")
    else:
        filled = {}
        for key, value in re.findall(r" --subst-var-by (\S+) (\S+)", m.group(1)):
            if key in filled:
                problems.append(f"flake.nix: fills @{key}@ twice")
            filled.setdefault(key, value)
for key in sorted(used | set(filled)) if filled is not None else ():
    if key not in filled:
        problems.append(f"flake.nix: the templates use @{key}@ and nothing fills it")
    elif key not in used:
        problems.append(f"flake.nix: fills @{key}@, which no template uses")
    elif key not in FILL:
        problems.append(f"flake.nix: fills @{key}@, which this check does not pin")
    elif filled[key] != FILL[key]:
        problems.append(f"flake.nix: @{key}@ is {filled[key]}, not {FILL[key]}")
m = re.search(r"^''\)\n\[\n((?:\"[^\"\n]*\"\n)*)\]$", text, re.M)
listed = re.findall(r'"([^"]*)"', m.group(1)) if m else None
if block and (listed is None or sorted(listed) != sorted(names)):
    problems.append(f"flake.nix: the wrappers built are {listed}, not {sorted(names)}")
if len(problems) == before and block != BINDING:
    n = next((k for k, (a, b) in enumerate(zip(block, BINDING)) if a != b), min(len(block), len(BINDING)))
    got = block[n] if n < len(block) else "(the end)"
    want = BINDING[n] if n < len(BINDING) else "(the end)"
    problems.append(f"flake.nix: the tail-wrappers binding's line {n + 1} is {got!r}, not {want!r}")

for p in problems:
    print(f"adk-nix: tail wrappers: {p}", file=sys.stderr)
sys.exit(1 if problems else 0)
EOF
adk_nix_note "tail wrappers: $names, each in the image's shape, naming only store paths, the venv and tools/ roots"
