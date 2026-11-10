#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Write the build context record of a devShells.build evidence shell, after
nix/build-env.sh has written the launcher and before anything runs, and
refuse (exit 1) when the context is incomplete.

capture_context.py has already recorded the CLI shell. This adds what a die
build runs, asked of the launcher itself rather than of this interpreter,
which is not the one LibreLane uses:

  - the launcher: path, sha256 (read again and held against the one
    exported), mode, text, the bash it runs under, the LibreLane it execs and
    the source roots it puts on the import path; the bash must be in the
    realised closure, the LibreLane the flake's, the roots exactly the
    directories build-env.sh takes from the trees the CLI shell declared
    (src, and chiplet-spec's reference/python);
  - LibreLane and the PDK: store path, narHash from the realised closure
    and the flake.lock inputs they were made from, held against what each
    states. For LibreLane that is the identity the flake evaluated (its
    adkIdentity, exported by devShells.build as ADK_NIX_LIBRELANE_IDENTITY):
    the ordered patches applied, by file and sha256, and the pin's
    LIBRELANE_PATCHED_REV, which is recorded as a claim: nothing here
    compares its tree (check-build-pins.sh --realize compares the patched
    source, and says when that commit went unchecked). For the PDK it is the
    statement the materialization wrote at the tree's root, pdk-source.json,
    recorded as pdk.source {path, sha256} of the bytes read: it must be
    materialized at the exported PDK path, name the lock's parent and
    submodule revisions and narHashes (a stale lock under --override-input
    says otherwise), and account, with the paths it left out, for every
    submodule the tree's .gitmodules names. The chiplet-flow build record
    names the same statement's sha256; the release record joins the two;
  - the cores' interpreter (ADK_NIX_LIBRELANE_PYTHON), recorded as
    librelane.interpreter: the file, reached through no link, in the bin of
    one realised store path, bound to that path and its narHash; and the
    package it serves, which must be exactly LibreLane's own (every
    site-packages/librelane under it resolves there, and the interpreter,
    run as a core launch runs it, finds librelane there and names itself as
    sys.executable). The shell's own Python serves none and is refused;
  - a probe: the launcher run with `--version` and PYTHONVERBOSE=1, as the
    flow starts it. From its output: the LibreLane version and the plugins it
    discovered, which must be librelane_plugin_chiplet alone, and from the
    interpreter's import trace every file it loaded code from.
    chiplet_die_flow, librelane_plugin_chiplet and chiplet_format_io must
    each be loaded, and only from their own root (the record names them,
    probe.required_at_start); every other file must lie in the realised
    closure or a root, so the system description, which the die flow does not
    import, is refused like any other file from outside them; a module loaded
    by any loader but the standard ones is refused, since it would load code
    without naming its file.

A capture, not a qualification: it shows what the launcher imports when it
starts, not what a die build later executes.

Usage:
    capture_build_context.py <out.json>
"""

import ast
import glob
import hashlib
import json
import os
import re
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import capture_context as cc  # noqa: E402

FORMAT = "adk-tools-nix-build-context/v0"
PLUGIN = "librelane_plugin_chiplet"
# Package -> the tree whose launcher root the launcher must import it from.
PACKAGES = {"chiplet_die_flow": "chiplet-flow", PLUGIN: "chiplet-flow",
            "chiplet_format_io": "chiplet-spec"}
# Tree -> the directory in it that is its launcher root: build-env.sh's
# ADK_NIX_LAUNCHER_TREES, which test_capture_build_context.py holds this to.
ROOT_DIR = {"chiplet-flow": "src", "chiplet-spec": "reference/python"}
# The packages the launcher must load when it starts, recorded as
# probe.required_at_start: all of them. The die flow's builders import
# chiplet_format_io when the package loads.
AT_START = ("chiplet_die_flow", PLUGIN, "chiplet_format_io")
# The PDK materialization's statement of its sources (flake.nix, sg13g2-pdk):
# its file at the tree's root, its format, and the inputs it names.
PDK_SOURCE = "pdk-source.json"
PDK_SOURCE_FORMAT = "adk-tools-pdk-source/v1"
PDK_PARENT = "ihp-open-pdk"
PDK_SUBMODULES = ("pycell4klayout-api", "pypreprocessor")
LOCK_INPUTS = {"librelane": ["librelane"], "pdk": [PDK_PARENT, *PDK_SUBMODULES]}
LIBRELANE_IDENTITY = "ADK_NIX_LIBRELANE_IDENTITY"
LIBRELANE_PYTHON = "ADK_NIX_LIBRELANE_PYTHON"
# How closureInfo's registration writes a narHash: sha256, in Nix's base32.
NAR_HASH = re.compile(r"sha256:[0-9abcdfghijklmnpqrsvwxyz]{52}")
PROBE_TIMEOUT = 120
# The cores' interpreter is asked as a core launch runs it: `-B -s`, with
# these variables unset (and every NIX_PYTHON* one) and PYTHONNOUSERSITE=1.
# It prints where it runs from and the file an import of librelane would
# load, found without importing it.
CORE_UNSET = ("BASH_ENV", "ENV", "PYTHONPATH")
CORE_UNSET_PREFIX = "NIX_PYTHON"
INTERPRETER_PROBE = (
    "import importlib.util, json, sys\n"
    "spec = importlib.util.find_spec('librelane')\n"
    "print(json.dumps({'executable': sys.executable,\n"
    "                  'origin': spec.origin if spec else None,\n"
    "                  'locations': list(spec.submodule_search_locations or []) if spec else None}))\n")

# The lines PYTHONVERBOSE=1 prints for a file it loads code from (CPython
# 3.13 importlib), with the groups that name a file: a source file is named
# unquoted, a bytecode file and an extension module by repr(), which quotes
# with " when the path holds a '.
_QUOTED = r"""('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")"""
_LOADED = [(re.compile(rf"^# code object from {_QUOTED}$"), (1,)),
           (re.compile(r"^# code object from (/.+)$"), (1,)),
           (re.compile(r"^# (/\S+) matches (/\S+)$"), (1, 2)),
           (re.compile(rf"^# extension module {_QUOTED} loaded from {_QUOTED}$"), (2,)),
           (re.compile(rf"^# extension module {_QUOTED} executed from {_QUOTED}$"), ())]
# A line that starts as one of those but that none of them reads: it may name
# a file, so it is refused, never passed over.
_LOADING = re.compile(r"^# (code object from |extension module |.* matches )")
_IMPORT = re.compile(r"^import '([^']+)' # (.*)$")
_LOADERS = re.compile(
    r"^(<class '_frozen_importlib\.(BuiltinImporter|FrozenImporter)'>"
    r"|<_frozen_importlib_external\.(SourceFileLoader|SourcelessFileLoader"
    r"|ExtensionFileLoader|_?NamespaceLoader) object at 0x[0-9a-f]+>)$")


def parse_launcher(text):
    """{"interpreter", "pythonpath": [...], "exec_shell", "execs"} from the
    launcher's text, read by the lines build-env.sh writes; a line missing is
    None."""
    lines = text.splitlines()
    m = re.fullmatch(r"#!(/\S+) -p", lines[0]) if lines else None
    interpreter = m.group(1) if m else None
    pythonpath = exec_shell = execs = None
    for line in lines:
        m = re.fullmatch(r"export PYTHONPATH='([^']*)'", line)
        if m:
            pythonpath = m.group(1).split(":") if m.group(1) else []
        m = re.fullmatch(r"""exec '([^']+)' -p -e '([^']+)' "\$@\"""", line)
        if m:
            exec_shell, execs = m.groups()
    return {"interpreter": interpreter, "pythonpath": pythonpath, "exec_shell": exec_shell,
            "execs": execs}


def parse_trace(stderr):
    """(files, other loaders, unread, rest) from a PYTHONVERBOSE=1 stderr:
    every file it names as loaded, every import by a loader outside _LOADERS,
    the lines that start as a load but could not be read, and the lines that
    are not the trace's."""
    files, loaders, unread, rest = [], [], [], []
    for line in stderr.splitlines():
        for pattern, groups in _LOADED:
            m = pattern.match(line)
            if m:
                files.extend(ast.literal_eval(g) if g[0] in "'\"" else g
                             for g in (m.group(i) for i in groups))
                break
        else:
            m = _IMPORT.match(line)
            if m:
                if not _LOADERS.match(m.group(2)):
                    loaders.append(line)
            elif _LOADING.match(line):
                unread.append(line)
            elif not line.startswith("# "):
                rest.append(line)
    return files, loaders, unread, rest


def parse_version(stdout):
    """(version, [plugin names]) from `librelane --version`."""
    version = None
    plugins = []
    in_plugins = False
    for line in stdout.splitlines():
        m = re.match(r"^LibreLane v(\S+)$", line.strip())
        if m and version is None:
            version = m.group(1)
        if line.strip() == "Discovered plugins:":
            in_plugins = True
        elif in_plugins and line.strip():
            plugins.append(line.strip().split(" -> ")[0])
    return version, plugins


def classify(real, roots):
    """(package, from its root) for a loaded file: which of our packages it
    belongs to, None when none, judged below the root when it lies in one, so
    the checkout's own path cannot count; a module file named like a package
    (chiplet_die_flow.py) counts as that package."""
    for pkg, root in roots.items():
        if cc.inside(real, os.path.join(root, pkg)):
            return pkg, True
    rel = real
    for root in roots.values():
        if cc.inside(real, root):
            rel = os.path.relpath(real, root)
            break
    for part in rel.split("/"):
        if part.split(".", 1)[0] in PACKAGES:
            return part.split(".", 1)[0], False
    return None, False


def origin_problems(files, roots, closure_paths):
    """Our packages each loaded, and only from their roots; nothing else from
    outside the realised closure and the roots. roots: package -> the
    realpath of its launcher root."""
    problems = []
    seen = set()
    for real in sorted({os.path.realpath(f) for f in files}):
        pkg, own = classify(real, roots)
        if pkg is not None:
            if own:
                seen.add(pkg)
            else:
                problems.append(f"{pkg} loaded from {real}, not from {roots[pkg]}")
        elif not any(cc.inside(real, p) for p in list(closure_paths) + list(roots.values())):
            problems.append(f"{real} loaded from outside the realised closure and the roots")
    for pkg in sorted(set(AT_START) - seen):
        problems.append(f"{pkg} was not loaded from {roots[pkg]} by the launcher")
    return problems


def lock_inputs(path, names):
    """{name: locked} for the flake's own inputs, resolved through the root
    node, so a node of another flake's inputs cannot stand in for one."""
    with open(path) as handle:
        lock = json.load(handle)
    nodes = lock["nodes"]
    root = nodes[lock["root"]]["inputs"]
    found = {}
    for name in names:
        key = root.get(name)
        if not isinstance(key, str):
            raise KeyError(f"{name} is not a direct input of the flake")
        locked = nodes[key]["locked"]
        if not re.fullmatch(r"[0-9a-f]{40}", str(locked.get("rev", ""))) or \
                not str(locked.get("narHash", "")).startswith("sha256-"):
            raise ValueError(f"{name} is locked without a full rev and narHash")
        found[name] = {k: locked[k] for k in ("type", "owner", "repo", "url", "rev", "narHash")
                       if k in locked}
    return found


def _same(what, evaluated, locked):
    """Problems where an evaluated {rev, narHash} is not the lock's."""
    if not isinstance(evaluated, dict) or not isinstance(locked, dict):
        return [f"{what}: no evaluated or locked rev and narHash"]
    return [f"{what}: evaluated {k} {evaluated.get(k)!r}, locked {locked.get(k)!r}"
            for k in ("rev", "narHash") if not evaluated.get(k) or evaluated.get(k) != locked.get(k)]


def identity_problems(identity, locked):
    """What LibreLane's evaluated identity leaves out, or says otherwise than
    the lock. identity: the parsed adkIdentity; locked: lock_inputs()."""
    if not isinstance(identity, dict):
        return ["librelane: the evaluated identity is not an object"]
    problems = _same("librelane input", identity.get("input"), locked.get("librelane"))
    patches = identity.get("patches")
    if not isinstance(patches, list):
        problems.append("librelane: the evaluated identity names no patch list")
    else:
        for i, patch in enumerate(patches):
            if not (isinstance(patch, dict) and re.fullmatch(r"[^/]+", str(patch.get("file", "")))
                    and re.fullmatch(r"[0-9a-f]{64}", str(patch.get("sha256", "")))):
                problems.append(f"librelane: patch {i} has no file name and sha256: {patch!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", str(identity.get("patchedRev", ""))):
        problems.append("librelane: the evaluated identity names no LIBRELANE_PATCHED_REV")
    return problems


def _not_relative(path):
    """Why path is not a canonical relative path, or None."""
    if not isinstance(path, str) or not path:
        return "not a nonempty string"
    if path.startswith("/"):
        return "absolute"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "not canonical: an empty, '.' or '..' component"
    return None


def _within(inner, outer):
    """inner is outer or lies under it, component by component."""
    return inner.split("/")[:len(outer.split("/"))] == outer.split("/")


def canonical_json(value):
    """The bytes builtins.toJSON gives value, and one newline: sorted keys,
    no whitespace."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode()


def gitmodule_paths(text):
    """The submodule paths a .gitmodules text names (git reads the key in
    any case)."""
    return re.findall(r"(?mi)^\s*path\s*=\s*(\S(?:.*\S)?)\s*$", text)


def read_pdk_source(pdk, problems):
    """({path, sha256}, statement, parsed) of the tree's pdk-source.json, the
    digest and the statement both from the one read; parsed is False when the
    file cannot be read or is not JSON (a JSON null parses). Appends to
    problems."""
    path = os.path.join(pdk, PDK_SOURCE) if pdk else None
    source = {"path": path, "sha256": None}
    if not path:
        return source, None, False
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            problems.append(f"pdk: {path} is not a regular file")
            return source, None, False
        with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
            if os.fstat(handle.fileno()).st_ino != st.st_ino:
                problems.append(f"pdk: {path} changed while read")
                return source, None, False
            raw = handle.read()
    except OSError as exc:
        problems.append(f"pdk: {path} unreadable: {exc.strerror or exc}")
        return source, None, False
    if st.st_mode & 0o222:
        problems.append(f"pdk: {path} is writable ({oct(stat.S_IMODE(st.st_mode))})")
    source["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        statement = json.loads(raw)
    except ValueError as exc:
        problems.append(f"pdk: {path} is not JSON: {exc}")
        return source, None, False
    if raw != canonical_json(statement):
        problems.append(f"pdk: {path} is not sorted-key compact JSON ending in one newline")
    return source, statement, True


def statement_problems(statement, pdk, locked):
    """What the PDK statement says otherwise than the exported PDK path, the
    lock, or the tree it sits in. locked: lock_inputs(), or its unavailable
    form."""
    if not isinstance(statement, dict):
        return ["pdk: the statement is not an object"]
    problems = []
    keys = ["format", "materialized_at", "not_fetched", "parent", "submodules"]
    if sorted(statement) != keys:
        problems.append(f"pdk: the statement's keys are {sorted(statement)}, not {keys}")
    if statement.get("format") != PDK_SOURCE_FORMAT:
        problems.append(f"pdk: the statement's format is {statement.get('format')!r}, "
                        f"not {PDK_SOURCE_FORMAT!r}")
    if statement.get("materialized_at") != pdk:
        problems.append(f"pdk: the statement was materialized at "
                        f"{statement.get('materialized_at')!r}, not at the exported {pdk!r}")
    parent = statement.get("parent")
    if not isinstance(parent, dict) or sorted(parent) != ["input", "narHash", "rev"] \
            or parent.get("input") != PDK_PARENT:
        problems.append(f"pdk: the statement's parent is not {{input: {PDK_PARENT!r}, rev, "
                        f"narHash}}: {parent!r}")
    else:
        problems += _same("pdk parent", parent, locked.get(PDK_PARENT))
    subs = statement.get("submodules")
    subs = subs if isinstance(subs, list) else [None]
    fetched = []
    for i, sub in enumerate(subs):
        if not isinstance(sub, dict) or sorted(sub) != ["input", "narHash", "path", "rev"] \
                or sub.get("input") not in PDK_SUBMODULES:
            problems.append(f"pdk: the statement's submodule {i} is not {{path, input, rev, "
                            f"narHash}} of one of {list(PDK_SUBMODULES)}: {sub!r}")
            continue
        fetched.append(sub["path"])
        problems += _same(f"pdk submodule {sub['input']}", sub, locked.get(sub["input"]))
    named = sorted(str(s["input"]) for s in subs if isinstance(s, dict) and "input" in s)
    if named != sorted(PDK_SUBMODULES):
        problems.append(f"pdk: the statement's submodules name {named}, "
                        f"not {sorted(PDK_SUBMODULES)} once each")
    not_fetched = statement.get("not_fetched")
    if not isinstance(not_fetched, list):
        problems.append(f"pdk: the statement's not_fetched is not a list: {not_fetched!r}")
        not_fetched = []
    for what, paths in (("submodule", fetched), ("not_fetched", not_fetched)):
        for path in paths:
            why = _not_relative(path)
            if why:
                problems.append(f"pdk: the statement's {what} path {path!r} is {why}")
        if all(isinstance(p, str) for p in paths) and paths != sorted(set(paths)):
            problems.append(f"pdk: the statement's {what} paths are not sorted and unique")
    fetched = [p for p in fetched if not _not_relative(p)]
    not_fetched = [p for p in not_fetched if not _not_relative(p)]
    every = fetched + not_fetched
    overlap = sorted({(a, b) for i, a in enumerate(every) for j, b in enumerate(every)
                      if i != j and _within(a, b)})
    if overlap:
        problems.append(f"pdk: the statement's gitlink paths overlap: {overlap}")
    if not pdk:
        return problems
    # The tree the statement sits in, at the paths that passed as relative.
    try:
        with open(os.path.join(pdk, ".gitmodules")) as handle:
            declared = sorted(set(gitmodule_paths(handle.read())))
    except OSError as exc:
        return problems + [f"pdk: the tree's .gitmodules unreadable: {exc.strerror or exc}"]
    if declared != sorted(set(every)):
        problems.append(f"pdk: the tree's .gitmodules names {declared}; the statement "
                        f"accounts for {sorted(set(every))}")
    for path in fetched:
        full = os.path.join(pdk, path)
        if os.path.islink(full) or not os.path.isdir(full) or not os.listdir(full):
            problems.append(f"pdk: the statement's submodule {path} is not a filled directory "
                            f"in the tree")
    for path in not_fetched:
        full = os.path.join(pdk, path)
        if os.path.lexists(full) and (os.path.islink(full) or not os.path.isdir(full)
                                      or os.listdir(full)):
            problems.append(f"pdk: {path} is left out by the statement but not empty in the tree")
    return problems


def librelane_package(librelane, problems):
    """The one librelane package the LibreLane store path installs (its
    lib/python3.*/site-packages/librelane), or None; appends to problems."""
    found = sorted(p for p in glob.glob(os.path.join(glob.escape(librelane), "lib", "python3.*",
                                                     "site-packages", "librelane"))
                   if os.path.isfile(os.path.join(p, "__init__.py")))
    if len(found) != 1:
        problems.append(f"librelane: {librelane} installs {len(found)} librelane packages, not one")
        return None
    if os.path.realpath(found[0]) != found[0]:
        problems.append(f"librelane: its package {found[0]} is reached through a link")
        return None
    return found[0]


def interpreter_record(interpreter, librelane, realised, env, problems):
    """The cores' interpreter's record; appends to problems. It must be a
    regular file, reached through no link, in the bin of one realised store
    path with a narHash, and serve exactly LibreLane's own package: every
    lib/python3.*/site-packages/librelane under that store path resolves to
    it, and the interpreter, run as a core launch runs it, finds librelane
    there and reports itself as sys.executable."""
    record = {"path": interpreter}
    if not interpreter:
        return record
    entry = next((p for p in realised if p["path"] != interpreter
                  and cc.inside(interpreter, p["path"])), None)
    if entry is None:
        problems.append(f"librelane interpreter {interpreter!r} is not in the realised closure")
    else:
        record.update(store_path=entry["path"], closure=entry)
        if not NAR_HASH.fullmatch(str(entry.get("narHash", ""))):
            problems.append(f"librelane interpreter's store path {entry['path']} has no narHash "
                            f"in the realised closure")
        if os.path.dirname(interpreter) != os.path.join(entry["path"], "bin"):
            problems.append(f"librelane interpreter {interpreter} is not in {entry['path']}/bin")
    try:
        st = os.lstat(interpreter)
    except OSError as exc:
        problems.append(f"librelane interpreter {interpreter}: {exc.strerror or exc}")
        return record
    record["mode"] = oct(stat.S_IMODE(st.st_mode))
    if not stat.S_ISREG(st.st_mode) or os.path.realpath(interpreter) != interpreter:
        problems.append(f"librelane interpreter {interpreter} is a link, is reached through "
                        f"one, or is not a regular file")
        return record
    if not st.st_mode & 0o111:
        problems.append(f"librelane interpreter {interpreter} is not executable")
    record["sha256"] = cc.sha256_file(interpreter)

    package = librelane_package(librelane, problems) if librelane else None
    root = os.path.dirname(os.path.dirname(interpreter))
    links = sorted(glob.glob(os.path.join(glob.escape(root), "lib", "python3.*", "site-packages",
                                          "librelane")))
    resolved = [os.path.realpath(p) for p in links]
    record["serves"] = {"package": package, "package_store_path": librelane or None,
                        "found": links, "resolved": resolved}
    if package and resolved != [package]:
        problems.append(f"librelane interpreter {interpreter} serves {resolved}, "
                        f"not exactly {package}")

    unset = sorted(k for k in env if k in CORE_UNSET or k.startswith(CORE_UNSET_PREFIX))
    run_env = {k: v for k, v in env.items() if k not in unset}
    run_env["PYTHONNOUSERSITE"] = "1"
    probe = {"argv": [interpreter, "-B", "-s", "-c", INTERPRETER_PROBE], "unset": unset,
             "set": {"PYTHONNOUSERSITE": "1"}}
    record["probe"] = probe
    try:
        done = subprocess.run(probe["argv"], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=PROBE_TIMEOUT, env=run_env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        probe.update(exit=None, error=f"{exc.__class__.__name__}: {exc}")
        problems.append(f"librelane interpreter probe failed: {probe['error']}")
        return record
    probe.update(exit=done.returncode, stdout=done.stdout, stderr=done.stderr)
    try:
        said = json.loads(done.stdout)
    except ValueError:
        said = None
    if done.returncode != 0:
        problems.append(f"librelane interpreter probe exited {done.returncode}: "
                        f"{done.stderr.strip()[-300:]!r}")
        return record
    if not isinstance(said, dict):
        problems.append(f"librelane interpreter probe gave no answer: "
                        f"{done.stdout.strip()[-300:]!r}")
        return record
    probe["answer"] = said
    if said.get("executable") != interpreter:
        problems.append(f"librelane interpreter reports sys.executable "
                        f"{said.get('executable')!r}, not {interpreter!r}")
    origin, locations = said.get("origin"), said.get("locations")
    if not package:
        return record
    if not isinstance(origin, str) \
            or os.path.realpath(origin) != os.path.join(package, "__init__.py") \
            or not isinstance(locations, list) \
            or [os.path.realpath(str(p)) for p in locations] != [package]:
        problems.append(f"librelane interpreter imports librelane from {origin!r} "
                        f"({locations!r}), not from {package}")
    return record


def launcher_record(launcher, launcher_sha, librelane, trees, closure_paths, problems):
    """The launcher's record, and package -> root; appends to problems."""
    record = {"path": launcher, "sha256": launcher_sha}
    try:
        st = os.lstat(launcher)
        with open(launcher, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        problems.append(f"launcher {launcher}: {exc}")
        return record, {}
    text = data.decode("utf-8", "replace")
    parsed = parse_launcher(text)
    actual = hashlib.sha256(data).hexdigest()
    record.update(actual_sha256=actual, mode=oct(stat.S_IMODE(st.st_mode)), text=text, **parsed)
    if not stat.S_ISREG(st.st_mode):
        problems.append(f"launcher {launcher} is not a regular file")
    if st.st_mode & 0o222:
        problems.append(f"launcher {launcher} is writable ({oct(stat.S_IMODE(st.st_mode))})")
    if actual != launcher_sha:
        problems.append(f"launcher sha256 is {actual}, the shell exported {launcher_sha}")
    if parsed["execs"] != os.path.join(librelane, "bin", "librelane"):
        problems.append(f"launcher execs {parsed['execs']}, not {librelane}/bin/librelane")
    interpreter = parsed["interpreter"] or ""
    if not interpreter or parsed["exec_shell"] != interpreter:
        problems.append(f"launcher runs under {interpreter!r} -p and the wrapper under "
                        f"{parsed['exec_shell']!r} -p -e, not one bash for both")
    if not any(cc.inside(interpreter, p) for p in closure_paths):
        problems.append(f"launcher runs under {interpreter!r}, outside the realised closure")
    roots = {}
    for pkg, tree_name in PACKAGES.items():
        tree = trees.get(tree_name)
        if not tree:
            problems.append(f"the CLI shell declared no {tree_name} tree")
            continue
        roots[pkg] = os.path.realpath(os.path.join(tree, ROOT_DIR[tree_name]))
    want = sorted(set(roots.values()))
    if parsed["pythonpath"] is None or sorted(parsed["pythonpath"]) != want:
        problems.append(f"launcher's PYTHONPATH is {parsed['pythonpath']}, expected {want}")
    return record, roots


def probe_launcher(launcher, roots, closure_paths, env, problems):
    probe = {"argv": [launcher, "--version"], "env": {"PYTHONVERBOSE": "1"},
             "required_at_start": list(AT_START)}
    try:
        done = subprocess.run([launcher, "--version"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                              env=dict(env, PYTHONVERBOSE="1"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        probe.update(exit=None, error=f"{exc.__class__.__name__}: {exc}")
        problems.append(f"launcher probe failed: {probe['error']}")
        return probe
    files, loaders, unread, rest = parse_trace(done.stderr)
    version, plugins = parse_version(done.stdout)
    real = sorted({os.path.realpath(f) for f in files})
    probe.update(exit=done.returncode, stdout=done.stdout, stderr_other=rest,
                 stderr_sha256=hashlib.sha256(done.stderr.encode()).hexdigest(),
                 version=version, plugins=plugins, other_loaders=loaders, unread_trace=unread,
                 loaded_files=real,
                 loaded={pkg: [f for f in real if classify(f, roots) == (pkg, True)]
                         for pkg in PACKAGES})
    if done.returncode != 0:
        problems.append(f"launcher --version exited {done.returncode}")
    if not version:
        problems.append("launcher --version printed no LibreLane version")
    if plugins != [PLUGIN]:
        problems.append(f"LibreLane discovered {plugins}, expected [{PLUGIN!r}]")
    for line in loaders:
        problems.append(f"loaded by a loader that names no file: {line}")
    for line in unread:
        problems.append(f"a trace line that names a load could not be read: {line}")
    problems += origin_problems(files, roots, closure_paths)
    return probe


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = argv[1]
    if os.path.lexists(out):
        print(f"adk-nix: {out} exists; a build context record is written once", file=sys.stderr)
        return 1
    env = dict(os.environ)
    problems = []

    librelane = env.get("ADK_NIX_LIBRELANE") or ""
    pdk = env.get("ADK_NIX_SG13G2_PDK") or ""
    launcher = env.get("CHIPLET_FLOW_LIBRELANE_LAUNCHER") or ""
    launcher_sha = env.get("CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256") or ""
    for name, value in (("ADK_NIX_LIBRELANE", librelane), ("ADK_NIX_SG13G2_PDK", pdk),
                        ("CHIPLET_FLOW_LIBRELANE_LAUNCHER", launcher),
                        ("CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256", launcher_sha)):
        if not value:
            problems.append(f"{name} is not set")
    if not pdk or env.get("PDK_ROOT") != pdk or env.get("PDK") != "ihp-sg13g2":
        problems.append(f"PDK_ROOT={env.get('PDK_ROOT')!r} PDK={env.get('PDK')!r}, "
                        f"expected {pdk!r} and 'ihp-sg13g2'")

    realised = cc.unreadable("closure", cc.closure, env.get("ADK_NIX_CLOSURE_INFO"))
    if "unavailable" in realised:
        problems.append(f"closure: {realised['unavailable']}")
    # Every entry, registered or not: a registered row can still carry an
    # empty or malformed hash.
    unhashed = [p["path"] for p in realised.get("paths", [])
                if not NAR_HASH.fullmatch(str(p.get("narHash", "")))]
    if unhashed:
        problems.append(f"closure: {len(unhashed)} paths without a sha256 narHash, "
                        f"first {unhashed[0]}")
    closure_paths = [p["path"] for p in realised.get("paths", [])]
    lock_path = env.get("ADK_NIX_FLAKE_LOCK")
    stores = {}
    for what, path in (("librelane", librelane), ("pdk", pdk)):
        entry = next((p for p in realised.get("paths", []) if path and p["path"] == path), None)
        if entry is None:
            problems.append(f"{what} {path!r} is not a path of the realised closure")
        elif not NAR_HASH.fullmatch(str(entry.get("narHash", ""))):
            problems.append(f"{what} {path!r} has no narHash in the realised closure")
        locked = cc.unreadable("flake lock", lambda p: lock_inputs(p, LOCK_INPUTS[what]), lock_path)
        if "unavailable" in locked:
            problems.append(f"{what}: {locked['unavailable']}")
        stores[what] = {"store_path": path, "closure": entry, "lock": locked}

    raw, identity = env.get(LIBRELANE_IDENTITY), None
    if not raw:
        problems.append(f"{LIBRELANE_IDENTITY} is not set")
    else:
        try:
            identity = json.loads(raw)
        except ValueError as exc:
            problems.append(f"{LIBRELANE_IDENTITY} is not JSON: {exc}")
        else:
            # Any decoded value, null included; only an object passes.
            problems += identity_problems(identity, stores["librelane"]["lock"])
    stores["librelane"]["evaluated_identity"] = identity
    interpreter = env.get(LIBRELANE_PYTHON) or ""
    if not interpreter:
        problems.append(f"{LIBRELANE_PYTHON} is not set")
    stores["librelane"]["interpreter"] = interpreter_record(
        interpreter, librelane, realised.get("paths", []), env, problems)
    if isinstance(identity, dict):
        stores["librelane"]["patches_applied"] = identity.get("patches")
        stores["librelane"]["patched_rev_claim"] = {
            "rev": identity.get("patchedRev"), "verified_here": False,
            "note": "staging.pin's LIBRELANE_PATCHED_REV, a historical commit; this record "
                    "does not compare its tree"}

    source, statement, parsed = read_pdk_source(pdk, problems)
    if parsed:
        problems += statement_problems(statement, pdk, stores["pdk"]["lock"])
    stores["pdk"].update(source=source, statement=statement)

    trees = cc.unreadable("ADK_NIX_TREES", cc.declared_trees, env.get("ADK_NIX_TREES"))
    if "unavailable" in trees:
        problems.append(trees.pop("unavailable"))
    record_launcher, roots, probe = {"path": launcher, "sha256": launcher_sha}, {}, None
    if launcher:
        record_launcher, roots = launcher_record(launcher, launcher_sha, librelane, trees,
                                                 closure_paths, problems)
    if launcher and len(roots) == len(PACKAGES):
        probe = probe_launcher(launcher, roots, closure_paths, env, problems)
    else:
        problems.append("launcher probe not run: no launcher, or a tree undeclared")

    record = {"format": FORMAT, "problems": problems, "launcher": record_launcher,
              "roots": roots, "librelane": stores["librelane"], "pdk": stores["pdk"],
              "closure": {k: realised.get(k) for k in
                          ("closure_info", "registration_sha256", "unavailable")},
              "sources": {name: cc.git_state(tree) for name, tree in sorted(trees.items())
                          if name in PACKAGES.values()},
              "probe": probe,
              "note": "a capture of what the launcher imports when it starts; "
                      "not a qualification of any build"}
    with open(out, "x") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    for problem in problems:
        print(f"adk-nix: build context: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
