#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The kinds packaging-flow ships can run in this image.

packaging-flow's verify-run keys each kind's tools from this image's tool
identity table, runs a run_here kind's tool at the path that table gives it,
in an environment it builds from nothing, and runs DRC decks found below a PDK
root the image sets. Three frames, each printed line by line:

  rows   every tool a shipped kind names (its ENTRY.tools) has a row in the
         table that keys as content (key.tool_resolved);
  exec   the argv[0] of each run_here kind, built by the kind's own argv
         builder from key.tool_paths over the table, as the executor builds
         it, answers its version flag in an empty environment (env -i); when
         the kind names klayout-python, the interpreter argv[0] runs in also
         imports klayout.db in that environment, which no version flag does.
         argv[0] is a tool's binary, or the venv's python as the python row's
         tree names it (bin/python3 below it, never resolved: the row's binary
         is the system interpreter the venv links to). Then argv[1], when it
         is a tool's binary, is a script that interpreter runs, and answers
         its own flag there too, as it does after the python row's binary;
  decks  every deck a shipped kind names resolves to its file set, by the
         kind's own deck_files, below the PDK root variable the kind reads.

The kinds, the table reader and the deck readers are packaging-flow's own,
imported from the staged checkout the venv runs, never a copy. A kind this
check cannot read is a failure, never a skip: a run_here kind whose argv[0]
is no tool it keys, a tool with no version flag below, a klayout-python kind
whose interpreter cannot be told, a deck_files it cannot call.

Usage:
    check_kind_contracts.py [--table FILE]
        (default: /opt/adk-tools/tool-identity.json)
Exit 0 when every frame holds, 1 when one does not.
"""
import argparse
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from packaging_flow.verification import key, kinds
from packaging_flow.verification.registry import ORIGINS

TABLE = Path("/opt/adk-tools/tool-identity.json")
# The origin whose kinds run a tool here; registry.ORIGINS has to name it.
RUN_HERE = "run_here"
# The flag each tool a kind runs answers with a line and exit 0. chiplet-cosim
# has no version flag; --help is the cheapest answer it gives.
# Neither DRC runner has a version flag either, nor the carrier DRC gate;
# --help answers before KLayout starts.
VERSION_FLAGS = {"adk-drc": "--help", "adk-verify-carrier-drc": "--help",
                 "assembly-lvs": "--version", "chiplet-cosim": "--help",
                 "intm4tm2-run-drc": "--help", "klayout": "-v", "python": "-V"}
STAND_IN = Path("/nonexistent")
TIMEOUT_S = 60
# A kind that names this tool runs klayout.db inside argv[0]'s interpreter.
KLAYOUT_PYTHON = "klayout-python"
# The row whose tree is the venv, and the venv's interpreter below it.
PYTHON = "python"
VENV_PYTHON = Path("bin", "python3")
# Tools whose binary is a sh wrapper that execs the venv's python, so their
# interpreter is no #! line's: bin/adk-drc, on run_drc.py.
VENV_WRAPPERS = frozenset({"adk-drc"})
IMPORT = "import klayout.db"
# The most of a #! line the kernel reads (BINPRM_BUF_SIZE).
SHEBANG_MAX = 256


class StandIn(Mapping):
    """Inputs for an argv builder: a path below /nonexistent for every role it
    asks for. Only argv[0] is read, and no builder opens an input."""

    def __getitem__(self, role):
        return STAND_IN / "inputs" / str(role)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


def rows(shipped, table):
    names = {entry["name"]: entry for entry in table["tools"]}
    red = 0
    for (kind, origin), module in sorted(shipped.items()):
        for tool in module.ENTRY.tools:
            entry = names.get(tool)
            if entry is None:
                word, red = "no row", red + 1
            elif not key.tool_resolved(entry):
                word, red = "a placeholder row", red + 1
            else:
                word = "ok"
            print(f"rows: {kind} ({origin}) {tool}: {word}")
    return red


def first_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""


def last_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def answer(argv, pick=first_line):
    """rc and one output line of argv (the first, or what pick takes), run with
    no environment at all."""
    try:
        done = subprocess.run(argv, env={}, cwd="/", stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, errors="replace",
                              timeout=TIMEOUT_S)
    except OSError as exc:
        return None, f"not run: {exc.strerror}"
    except subprocess.TimeoutExpired:
        return None, f"no answer in {TIMEOUT_S} s"
    return done.returncode, pick(done.stdout) or pick(done.stderr)


def shebang(path):
    """The interpreter and its one optional argument the kernel runs path with,
    read from its #! line as the kernel reads it, or None and why there are none."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(SHEBANG_MAX)
    except OSError as exc:
        return None, f"unread: {exc.strerror}"
    if not head.startswith(b"#!"):
        return None, "no #! line"
    line = head[2:].split(b"\n", 1)[0].strip(b" \t")
    if not line:
        return None, "an empty #! line"
    words = re.split(rb"[ \t]+", line, maxsplit=1)
    return [os.fsdecode(word) for word in words], None


def venv_python(table):
    """The venv's interpreter as the python row's tree names it, or None."""
    row = next((entry for entry in table["tools"] if entry["name"] == PYTHON), None)
    if row is None or not key.tool_resolved(row) or not row.get("path"):
        return None
    return str(Path(row["path"]) / VENV_PYTHON)


def argv_tools(argv, paths, venv):
    """The tool argv[0] runs and the script tool argv[1] names, or None for the
    script: argv[0] as one keyed tool's binary, else as the venv's python when
    the kind keys python. Under python, by its row's binary or the venv's,
    argv[1] is a script when it is another keyed tool's binary. A (None, None)
    is a red."""
    if not argv:
        return None, None
    named = [name for name, path in paths.items() if str(path) == argv[0]]
    in_venv = not named and PYTHON in paths and venv is not None and argv[0] == venv
    if named == [PYTHON] or in_venv:
        scripts = [name for name, path in paths.items()
                   if name != PYTHON and len(argv) > 1 and str(path) == argv[1]]
        return PYTHON, scripts[0] if len(scripts) == 1 else None
    return (named[0], None) if len(named) == 1 else (None, None)


def interpreter_of(tool, argv0, venv):
    """The interpreter that runs klayout.db for a kind, and why there is none."""
    if tool == PYTHON:
        return [argv0], None
    if tool in VENV_WRAPPERS:
        return ([venv], None) if venv else (None, "no python row to name the venv by")
    return shebang(argv0)


def imports_klayout(kind, tool, argv0, venv):
    """Print whether the kind's interpreter imports klayout.db; 1 when it does not."""
    interpreter, why = interpreter_of(tool, argv0, venv)
    if interpreter is None:
        print(f"exec: {kind} {KLAYOUT_PYTHON} {argv0}: {why} -> red")
        return 1
    # A failed import ends its traceback with the error; that line is the answer.
    rc, line = answer(interpreter + ["-c", IMPORT], pick=last_line)
    ok = rc == 0
    print(f"exec: {kind} {KLAYOUT_PYTHON} {' '.join(interpreter)} -c \"{IMPORT}\": rc {rc}: "
          f"{line or '(no output)'}{'' if ok else ' -> red'}")
    return int(not ok)


def version_answer(kind, tool, argv):
    """Print argv's answer to tool's version flag; 1 when it gives none."""
    flag = VERSION_FLAGS.get(tool)
    if flag is None:
        print(f"exec: {kind} {tool}: no version flag known for it")
        return 1
    rc, line = answer(argv + [flag])
    ok = rc == 0 and bool(line)
    print(f"exec: {kind} {tool} {' '.join(argv)} {flag}: rc {rc}: {line or '(no output)'}"
          f"{'' if ok else ' -> red'}")
    return int(not ok)


def exec_frame(shipped, table):
    red = 0
    venv = venv_python(table)
    for (kind, origin), module in sorted(shipped.items()):
        entry = module.ENTRY
        if origin != RUN_HERE:
            continue
        paths = key.tool_paths(table, entry.tools)
        missing = sorted(name for name, path in paths.items() if path is None)
        if missing:
            print(f"exec: {kind}: argv[0] unbuilt, {', '.join(missing)} has no row that keys")
            red += 1
            continue
        builder = key.shipped(entry.argv_builder)
        if builder is None:
            print(f"exec: {kind}: its argv builder {entry.argv_builder} is not shipped")
            red += 1
            continue
        # A builder that declares tool_places is handed it, as the executor does.
        places = key.tool_places(table, entry.tools)
        try:
            argv = builder(StandIn(), STAND_IN / "outputs", {}, dict(paths),
                           variables=key.entry_variables(entry, os.environ),
                           **key.builder_keywords(builder, places))
        except Exception as exc:  # a builder that cannot run here is a failure
            print(f"exec: {kind}: its argv builder raised {type(exc).__name__}: {exc}")
            red += 1
            continue
        tool, script = argv_tools(argv, paths, venv)
        if tool is None:
            print(f"exec: {kind}: argv[0] {argv[0] if argv else None!r} is no one tool it keys")
            red += 1
            continue
        red += version_answer(kind, tool, argv[:1])
        if script is not None:
            red += version_answer(kind, script, argv[:2])
        if KLAYOUT_PYTHON in entry.tools:
            red += imports_klayout(kind, tool, argv[0], venv)
    return red


def decks(shipped):
    red = 0
    for (kind, origin), module in sorted(shipped.items()):
        if not hasattr(module, "deck_files"):
            continue
        variable = getattr(module, "PDK_ROOT", None)
        value = os.environ.get(variable) if isinstance(variable, str) else None
        if value is None:
            print(f"decks: {kind}: its PDK root variable {variable!r} is unset")
            red += 1
            continue
        root = Path(value)
        if hasattr(module, "DECKS"):
            found = [(f"{pdk} {deck}", module.deck_files(root.joinpath(pdk, *module.DECK_TREE),
                                                          deck))
                     for pdk, deck in sorted(module.DECKS.items())]
        elif hasattr(module, "DECK_FOLDER"):
            found = [(module.DECK, module.deck_files(root.joinpath(*module.DECK_FOLDER)))]
        else:
            print(f"decks: {kind}: deck_files with neither DECKS nor DECK_FOLDER")
            red += 1
            continue
        for name, files in found:
            if isinstance(files, key.Unresolved):
                print(f"decks: {kind} {name} below ${variable}: {files.why} -> red")
                red += 1
            else:
                print(f"decks: {kind} {name} below ${variable}: {len(files)} files")
    return red


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", type=Path, default=TABLE)
    args = parser.parse_args(argv)
    if RUN_HERE not in ORIGINS:
        print(f"kind contracts: FAIL, packaging-flow names no origin {RUN_HERE!r}")
        return 1
    if not hasattr(key, "builder_keywords"):
        print("kind contracts: FAIL, packaging-flow has no key.builder_keywords, "
              "which the exec frame hands argv builders by; "
              "pin a packaging-flow ref that carries it")
        return 1
    table, digest = key.load_tool_table(args.table.read_bytes())
    shipped = kinds.shipped()
    print(f"kind contracts: {len(shipped)} kinds, table {args.table} sha256 {digest}")
    red = {"rows": rows(shipped, table), "exec": exec_frame(shipped, table),
           "decks": decks(shipped)}
    word = "PASS" if not any(red.values()) else "FAIL"
    print(f"kind contracts: {word} " + " ".join(f"{frame} {n} red" for frame, n in red.items()))
    return 0 if word == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
