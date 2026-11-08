#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Write the context record of a devShells.cli evidence shell, before anything
runs in it, and refuse (exit 1) when the context is incomplete.

chiplet-flow records the commits of its own packages and the Python version.
It does not record the lock the environment came from, the store paths that
were realised, OpenROAD's build, or the third-party modules it imported
(jsonschema, for one). This file does, from inside the shell, by asking each
thing directly rather than trusting what the flake says it should be:

  - the flake revision and the lock it was evaluated with (sha256 and every
    locked input);
  - the realised closure, one entry per store path with its narHash, from the
    closureInfo the flake built for this shell;
  - the file, version and distribution of every module on the CLI path, as
    imported, and every distribution Python can see;
  - KLayout: the `klayout` on PATH and its `-b -v`, the file and version of
    the klayout.db the interpreter imports, and the declared skew against the
    KLayout the image runs;
  - z3: the version the library reports and the version its z3-solver
    metadata claims, which must agree;
  - the `assembly-lvs` on PATH, which lvs-verify runs and must be the venv's;
  - `openroad -version`, `git --version`, the interpreter, and the versions of
    setuptools and pip that did the install (read from their metadata, not
    imported: setuptools puts its vendored packages on sys.path when imported);
  - the executable of the shell that ran the hook, which is the bash
    `nix develop` chose (record only; nothing is checked against it);
  - each source tree and root: path, commit, clean or dirty;
  - the variables the hook set, sys.path, and the working directory the
    command will start in.

And it checks where things came from, not only what they are: every
third-party module must resolve inside the realised closure, every module of
ours inside the tree the hook verified for it, and nothing on sys.path or
among the distributions may sit outside the closure, the venv and those
trees. sys.path and the distributions are taken before anything is imported,
as the command will start with them; an entry an import adds is checked too.
The working directory must be the context directory and hold nothing else:
`python -m` puts it first on sys.path. A missing lock, closure, tool answer or
tree identity, or a tree that is not at the top of its own clean checkout, is
a problem too. The record is written either way, with the problems in it; the exit
status is what the hook acts on.

It is a sidecar next to a run, not part of the run manifest, and a capture,
not a launch: `nix develop` enforces no isolation, and the record says so.
The format name carries v0 because no reader has agreed to it yet.

Usage:
    capture_context.py <out.json>
"""

import datetime
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import site
import subprocess
import sys
import warnings

FORMAT = "adk-tools-nix-context/v0"

# Every module the ready-die CLI path imports that is not stdlib. The first
# group comes from Nix, the second is installed editable from a checkout.
THIRD_PARTY = ["yaml", "jsonschema", "attrs", "referencing", "rpds",
               "jsonschema_specifications", "klayout.db", "z3"]
# Used by the hook's install, never by the flow: versions only, no import.
INSTALL_TOOLS = ["setuptools", "pip"]
OURS = ["chiplet_die_flow", "packaging_flow", "interposer_pnr", "chiplet_format_io",
        "chiplet_system_description", "assembly_lvs", "chiplet_cosim"]

ROOTS = ["ADK_ROOT", "INTERPOSER_PDK_ROOT", "INTERCONNECT_PDK_ROOT", "CHIPLET_SPEC_ROOT"]

ENV_KEYS = ROOTS + [
    "ADK_NIX_MODE", "ADK_NIX_FLAKE_REV", "ADK_NIX_VENV", "ADK_NIX_PYTHON",
    "ADK_NIX_SITE_PACKAGES", "ADK_NIX_OPENROAD", "ADK_NIX_CLOSURE_INFO",
    "ADK_NIX_FLAKE_LOCK", "OPENROAD", "CHIPLET_FLOW_ENGINE_PYTHON",
    "CHIPLET_FLOW_THREAD_CAP", "CHIPLET_FLOW_READY_DIES", "HOME", "KLAYOUT_HOME",
    "XDG_CONFIG_HOME", "PYTHONNOUSERSITE", "PYTHONPATH", "PATH", "ADK_NIX_TREES",
    "ADK_NIX_ADK_TOOLS", "ADK_NIX_CONTEXT_OUT", "KLAYOUT_PATH", "CHIPLET_FLOW_PADMAP_PYTHON",
    "ADK_NIX_KLAYOUT", "ADK_NIX_KLAYOUT_IMAGE_VERSION", "CHIPLET_FLOW_ASSEMBLY_LVS",
    "COSIM_DEVSHELL", "ADK_TOOLS_TABLE", "ADK_TOOLS_CLOSURE",
]

# Every variable the hook scrubs for KLayout and does not set again (it sets
# KLAYOUT_HOME and KLAYOUT_PATH, recorded above): the ones that steer
# `klayout -b` or the klayout.db module, and the two that change only what it
# fetches or prints. Set here means something set it again after the hook.
KLAYOUT_STEERING = ["LD_LIBRARY_PATH", "RUBYOPT", "RUBYLIB", "KLAYOUT_PYTHONPATH",
                    "KLAYOUT_PYTHONHOME", "KLAYOUT_IGNORE_MACRO_DIRS",
                    "KLAYOUT_SALT_MINE", "KLAYOUT_VERBOSITY"]


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(argv):
    """{exit, stdout, stderr} of a short command; never raises. exit is None
    when the command could not be started or did not finish."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit": None, "stdout": "", "stderr": f"{exc.__class__.__name__}: {exc}"}
    return {"exit": done.returncode, "stdout": done.stdout.strip(),
            "stderr": done.stderr.strip()}


def git_state(path):
    """Asked the way chiplet-flow's provenance asks it. The hook has already
    held every tree to its pin with nothing able to hide a change; this is the
    record of what was found. A command that fails leaves its field
    unavailable (commit) or None (clean), never a guess."""
    top = run(["git", "-C", path, "rev-parse", "--show-toplevel"])
    if top["exit"] != 0:
        return {"path": path, "commit": "unavailable: not a git checkout"}
    commit = run(["git", "-C", path, "rev-parse", "HEAD"])
    status = run(["git", "-C", path, "status", "--porcelain", "--untracked-files=all"])
    return {"path": path, "toplevel": top["stdout"],
            "commit": commit["stdout"] if commit["exit"] == 0 and commit["stdout"]
            else "unavailable: rev-parse HEAD failed",
            "clean": status["stdout"] == "" if status["exit"] == 0 else None}


def checkout_problems(what, state):
    """What is wrong with a tree's recorded state: no commit, not the top of
    its own checkout (the directory is a subdirectory, or core.worktree points
    git elsewhere), or not clean."""
    if not isinstance(state, dict):
        return [f"{what}: no commit could be recorded"]
    if not state.get("commit") or state["commit"].startswith("unavailable"):
        return [f"{what}: no commit could be recorded"]
    problems = []
    if os.path.realpath(state["path"]) != os.path.realpath(state.get("toplevel") or ""):
        problems.append(f"{what}: {state['path']} is not the top of its checkout "
                        f"({state.get('toplevel')})")
    if state.get("clean") is not True:
        problems.append(f"{what}: {state['path']} is not clean")
    return problems


def store_path_of(path):
    """/nix/store/<hash-name> for a path inside the store, else None."""
    parts = path.split("/")
    if len(parts) > 3 and parts[0] == "" and parts[1] == "nix" and parts[2] == "store" and parts[3]:
        return "/".join(parts[:4])
    return None


def inside(path, root):
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def normalized(name):
    return name.lower().replace("_", "-").replace(".", "-")


def origin_problems(modules, trees, closure_paths, venv, own_dir, sys_path, dists,
                    own_dists=None):
    """Every place Python imports from, held against where it may.

    modules         name -> record with "realpath" (as module() writes it)
    trees           module name -> realpath of the verified tree it must
                    come from (ours only)
    closure_paths   the store paths of the realised closure
    venv            realpath of this shell's venv
    own_dir         realpath of this script's directory, on sys.path only
                    because Python puts it there
    sys_path        sys.path, realpaths
    dists           one {"name", "location", "version", "requires",
                    "entry_points"} per distribution on sys.path, location
                    being the realpath of the directory its metadata was
                    found in
    own_dists       normalized distribution name -> realpath of the verified
                    tree it is installed from (ours only)

    Third-party modules must resolve inside the closure, ours inside their
    tree; nothing else on sys.path or among the distributions may be outside
    the closure, the venv and the trees. A distribution found twice is
    shadowing, with one exception: an editable install of ours leaves its
    .dist-info in the venv and the .egg-info the build wrote in its tree, and
    Python sees both. That pair, and only that pair, passes: the same name,
    version, requirements and entry points, one in the venv and one in that
    package's own tree. Returns the list of violations.
    """
    closure_paths = set(closure_paths)
    problems = []

    def allowed(path):
        store = store_path_of(path)
        if store is not None:
            return store in closure_paths
        return inside(path, venv) or any(inside(path, tree) for tree in trees.values())

    for name, record in sorted(modules.items()):
        where = record.get("realpath")
        if where is None:
            if "import" not in record:  # an import failure is reported on its own
                problems.append(f"{name} has no file, so where it came from cannot be checked")
            continue
        if name in trees:
            if not inside(where, trees[name]):
                problems.append(f"{name} imported from {where}, not from its verified tree {trees[name]}")
        else:
            store = store_path_of(where)
            if store is None or store not in closure_paths:
                problems.append(f"{name} imported from {where}, outside the realised closure")
    for entry in sys_path:
        if entry == own_dir:
            continue
        if not allowed(entry):
            problems.append(f"sys.path entry {entry} is outside the closure, the venv and the trees")
    own_dists = own_dists or {}
    found = {}
    for dist in dists:
        found.setdefault(normalized(dist["name"]), []).append(dist)
        if not allowed(dist["location"]):
            problems.append(f"distribution {dist['name']} at {dist['location']} is outside "
                            f"the closure, the venv and the trees")
    for key, copies in sorted(found.items()):
        if len(copies) == 1:
            continue
        tree = own_dists.get(key)
        in_venv = [d for d in copies if inside(d["location"], venv)]
        in_tree = [d for d in copies if tree and inside(d["location"], tree)]
        same = len({(d["version"], tuple(sorted(d["requires"] or [])),
                     tuple(sorted(d.get("entry_points") or []))) for d in copies}) == 1
        if not (tree and len(copies) == 2 and len(in_venv) == 1 and len(in_tree) == 1 and same):
            places = ", ".join(f"{d['location']} ({d['version']})" for d in copies)
            problems.append(f"distribution {copies[0]['name']} found {len(copies)} times: {places}")
    return problems


def parse_registration(text):
    """Store path -> {narHash, narSize, references} from a closureInfo
    registration file.

    The file is the `nix-store --load-db` format: per path, the path, its hash,
    its size, its deriver (empty in closureInfo), a reference count and that
    many references. It is read by that structure, not by pattern, and any
    line that does not fit raises: a record with a hole in it is worse than
    none.
    """
    lines = text.splitlines()
    entries = {}
    i = 0
    while i < len(lines):
        if lines[i] == "" and all(line == "" for line in lines[i:]):
            break
        if i + 4 >= len(lines):
            raise ValueError(f"registration line {i + 1}: truncated entry")
        path, nar_hash, size, _deriver, count = lines[i:i + 5]
        if not path.startswith("/nix/store/"):
            raise ValueError(f"registration line {i + 1}: {path!r} is not a store path")
        if not (size.isdigit() and count.isdigit()):
            raise ValueError(f"registration line {i + 1}: bad size or reference count")
        refs = lines[i + 5:i + 5 + int(count)]
        if len(refs) != int(count):
            raise ValueError(f"registration line {i + 1}: truncated references")
        entries[path] = {"narHash": nar_hash, "narSize": int(size),
                         "references": len(refs)}
        i += 5 + int(count)
    return entries


def unreadable(what, read, arg):
    """read(arg), or {"unavailable": ...} when the file is missing or not in
    the shape expected: a problem to record, never a crash before the record
    is written."""
    try:
        return read(arg)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        return {"unavailable": f"{what} unreadable: {exc.__class__.__name__}: {exc}"}


def closure(info_dir):
    if not info_dir or not os.path.isdir(info_dir):
        return {"unavailable": f"no closureInfo at {info_dir!r}"}
    with open(os.path.join(info_dir, "store-paths")) as handle:
        paths = [p for p in handle.read().splitlines() if p]
    with open(os.path.join(info_dir, "registration")) as handle:
        try:
            registration = parse_registration(handle.read())
        except ValueError as exc:
            return {"closure_info": info_dir, "unavailable": f"registration unparsed: {exc}"}
    missing = [p for p in paths if p not in registration]
    record = {
        "closure_info": info_dir,
        "registration_sha256": sha256_file(os.path.join(info_dir, "registration")),
        "paths": [dict(path=p, **registration.get(p, {})) for p in paths],
    }
    if missing:
        record["paths_without_hash"] = missing
    total = os.path.join(info_dir, "total-nar-size")
    if os.path.isfile(total):
        with open(total) as handle:
            record["total_nar_size"] = int(handle.read().strip())
    return record


def flake_lock(path):
    if not path or not os.path.isfile(path):
        return {"unavailable": f"no flake.lock at {path!r}"}
    with open(path) as handle:
        lock = json.load(handle)
    inputs = {}
    for name, node in sorted(lock.get("nodes", {}).items()):
        locked = node.get("locked")
        if locked:
            inputs[name] = {k: locked[k] for k in
                            ("type", "owner", "repo", "url", "rev", "narHash")
                            if k in locked}
    return {"path": path, "sha256": sha256_file(path), "inputs": inputs}


def distribution_list():
    """Every distribution Python can see, as origin_problems() takes them."""
    found = []
    for d in importlib.metadata.distributions():
        found.append(dict(
            name=d.metadata.get("Name") or "?",
            location=os.path.realpath(str(d.locate_file(""))),
            version=d.version, requires=d.requires or [],
            entry_points=sorted(f"{e.group}:{e.name}={e.value}" for e in d.entry_points)))
    return found


def klayout_tools(env, closure_paths, which=shutil.which, store_of=store_path_of):
    """The KLayout this context runs, both ways the tail reaches it, and what is
    wrong with it: the `klayout` found on PATH (the DRC wrappers call it by
    name) must be the one the flake names and inside the closure, and answer
    `-b -v` with the version the imported klayout.db reports; the flow's
    klayout.db interpreter must be this venv's. The skew against the image is
    recorded as declared, and is not a problem by itself."""
    problems = []
    closure_paths = set(closure_paths)
    found = which("klayout", path=env.get("PATH", ""))
    declared = env.get("ADK_NIX_KLAYOUT") or ""
    binary = {"path": found, "realpath": os.path.realpath(found) if found else None,
              "declared": os.path.join(declared, "bin", "klayout") if declared else None}
    binary["version"] = run([found, "-b", "-v"]) if found else None
    if not declared:
        problems.append("ADK_NIX_KLAYOUT is not set")
    if not found:
        problems.append("no klayout on PATH")
    else:
        if declared and binary["realpath"] != os.path.realpath(binary["declared"]):
            problems.append(f"klayout on PATH is {binary['realpath']}, not the flake's {binary['declared']}")
        if store_of(binary["realpath"]) not in closure_paths:
            problems.append(f"klayout at {binary['realpath']} is outside the realised closure")
        if binary["version"]["exit"] != 0 or not binary["version"]["stdout"]:
            problems.append("klayout -b -v did not answer")
    try:
        import klayout
        import klayout.db
        # As the other module records: the file as imported, and where it
        # resolves. They differ in the shell (the Python output's
        # site-packages links into the KLayout output), and the imported path
        # is the only one that says which output the import went through.
        path = klayout.db.__file__
        module_record = {"file": path, "realpath": os.path.realpath(path),
                         "version": str(klayout.__version__)}
    except Exception as exc:  # the import itself is a problem already, by name
        module_record = {"import": f"failed: {exc.__class__.__name__}: {exc}"}
    shell_version = module_record.get("version")
    if found and shell_version and binary["version"]["exit"] == 0 \
            and shell_version not in binary["version"]["stdout"].split():
        problems.append(f"klayout -b -v says {binary['version']['stdout']!r}, "
                        f"klayout.db is {shell_version}")
    image = env.get("ADK_NIX_KLAYOUT_IMAGE_VERSION") or None
    if image is None:
        problems.append("ADK_NIX_KLAYOUT_IMAGE_VERSION is not set; the skew cannot be stated")
    skew = {"shell": shell_version, "image": image, "declared": True,
            "differs": None if not (shell_version and image) else shell_version != image}
    padmap = env.get("CHIPLET_FLOW_PADMAP_PYTHON") or ""
    venv = env.get("ADK_NIX_VENV") or ""
    if not venv or os.path.abspath(padmap) != os.path.join(os.path.abspath(venv), "bin", "python"):
        problems.append(f"CHIPLET_FLOW_PADMAP_PYTHON is {padmap!r}, not this venv's python")
    for name in KLAYOUT_STEERING:
        if env.get(name) is not None:
            problems.append(f"{name} is set in the shell; it steers KLayout")
    return {"binary": binary, "module": module_record, "skew": skew}, problems


def z3_solver(version_of=importlib.metadata.version):
    """The z3 the engine imports: the version the library reports must be the
    one its z3-solver metadata claims, which is what pip check read (the flake
    writes that metadata from the derivation's version). The image does not pin
    z3, so there is no image side to declare; the engine's report records the
    version each side ran."""
    try:
        import z3
    except Exception as exc:  # the import itself is a problem already, by name
        return {"import": f"failed: {exc.__class__.__name__}: {exc}"}, []
    try:
        library = str(z3.get_version_string())
    except Exception as exc:
        return {"library": f"failed: {exc.__class__.__name__}: {exc}"}, \
            [f"z3 imported but did not report its version: {exc.__class__.__name__}: {exc}"]
    try:
        distribution = version_of("z3-solver")
    except importlib.metadata.PackageNotFoundError:
        distribution = None
    problems = []
    if distribution != library:
        problems.append(f"z3 reports {library}, its z3-solver metadata "
                        f"{distribution if distribution else 'is missing'}")
    return {"library": library, "distribution": distribution}, problems


def assembly_lvs(env, which=shutil.which):
    """The assembly-lvs lvs-verify runs: chiplet-flow takes CHIPLET_FLOW_ASSEMBLY_LVS,
    which the hook clears, and otherwise `assembly-lvs` from PATH, which must be
    this venv's console script."""
    found = which("assembly-lvs", path=env.get("PATH", ""))
    venv = env.get("ADK_NIX_VENV") or ""
    declared = os.path.join(venv, "bin", "assembly-lvs") if venv else None
    problems = []
    if env.get("CHIPLET_FLOW_ASSEMBLY_LVS") is not None:
        problems.append(f"CHIPLET_FLOW_ASSEMBLY_LVS is set in the shell ({env['CHIPLET_FLOW_ASSEMBLY_LVS']}); "
                        f"lvs-verify would run it")
    if not found:
        problems.append("no assembly-lvs on PATH")
    elif declared is None or os.path.abspath(found) != os.path.abspath(declared):
        problems.append(f"assembly-lvs on PATH is {found}, not this venv's {declared}")
    return {"path": found, "declared": declared}, problems


def install_tools():
    versions = {}
    for name in INSTALL_TOOLS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable: no distribution metadata"
    return versions


def parent_shell():
    """The shell that started this script: the hook runs in the bash `nix
    develop` chose and starts this script as its child. Read from /proc, not
    from $SHELL, which inside `nix develop` names the shell derivation's build
    shell."""
    exe = f"/proc/{os.getppid()}/exe"
    try:
        return {"realpath": os.path.realpath(exe, strict=True)}
    except OSError as exc:
        return {"unavailable": f"{exe}: {exc.__class__.__name__}: {exc}"}


def module(name, dists):
    try:
        mod = importlib.import_module(name)
    except Exception as exc:  # recorded, and a problem below
        return {"import": f"failed: {exc.__class__.__name__}: {exc}"}
    top = name.split(".")[0]
    path = getattr(mod, "__file__", None)
    record = {"file": path, "realpath": os.path.realpath(path) if path else None}
    record["distributions"] = {}
    for dist in dists.get(top, []):
        try:
            record["distributions"][dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            record["distributions"][dist] = "unavailable: no distribution metadata"
    # Some packages deprecate the attribute (jsonschema 4.25 warns on it); the
    # version of record is the distribution metadata above, so a warning here
    # is noise on every entry and is silenced, not recorded. Read once:
    # hasattr and then the attribute would each raise any other warning.
    missing = object()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        version = getattr(mod, "__version__", missing)
        if version is not missing:
            record["__version__"] = str(version)
    return record


# Tree name (as the hook names it) -> the modules of ours that must come from
# it, and the distributions it installs (each install directory's pyproject
# [project] name). A tree pip installs from more than one directory carries
# one of each per directory: chiplet-spec, its reader (reference/python) and
# the system description (description).
TREE_MODULES = {"chiplet-flow": ("chiplet_die_flow",), "packaging-flow": ("packaging_flow",),
                "interposer-pnr": ("interposer_pnr",),
                "chiplet-spec": ("chiplet_format_io", "chiplet_system_description"),
                "assembly-lvs": ("assembly_lvs",), "chiplet-cosim": ("chiplet_cosim",)}
TREE_DISTS = {"chiplet-flow": ("chiplet-die-flow",), "packaging-flow": ("packaging_flow",),
              "interposer-pnr": ("interposer-pnr",),
              "chiplet-spec": ("chiplet-format-io", "chiplet-system-description"),
              "assembly-lvs": ("assembly-lvs",), "chiplet-cosim": ("chiplet-cosim",)}


def declared_trees(text):
    """"<name>|<tree>|<install dir>" lines, as the hook exports them, as
    name -> tree. A tree installed from two directories has two lines, which
    must name the same tree; a name given two trees raises ValueError."""
    trees = {}
    for line in (text or "").splitlines():
        if line.strip():
            name, tree, _sub = line.split("|")
            if trees.setdefault(name, tree) != tree:
                raise ValueError(f"{name} is declared as both {trees[name]} and {tree}")
    return trees


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = argv[1]
    if os.path.lexists(out):
        print(f"adk-nix: {out} exists; a context record is written once", file=sys.stderr)
        return 1
    problems = []

    # What the command will start with, before any import here can change it.
    path_at_start = list(sys.path)
    found_dists = distribution_list()
    cwd = os.getcwd()
    context_out = os.environ.get("ADK_NIX_CONTEXT_OUT") or ""
    if not context_out or os.path.realpath(cwd) != os.path.realpath(context_out):
        problems.append(f"working directory {cwd} is not the context directory {context_out!r}")
    else:
        # Before this record is written, so anything here is someone else's.
        stray = sorted(os.listdir(cwd))
        if stray:
            problems.append(f"context directory {cwd} is not empty: {', '.join(stray[:5])}")

    dists = importlib.metadata.packages_distributions()
    modules = {name: module(name, dists) for name in THIRD_PARTY + OURS}
    problems += [f"{name}: import {mod['import']}" for name, mod in sorted(modules.items())
                 if "import" in mod]
    path_added = [p for p in sys.path if p not in path_at_start]

    trees = unreadable("ADK_NIX_TREES", declared_trees, os.environ.get("ADK_NIX_TREES"))
    if "unavailable" in trees:
        problems.append(trees.pop("unavailable"))
    if set(trees) != set(TREE_MODULES):
        problems.append(f"ADK_NIX_TREES names {sorted(trees)}, expected {sorted(TREE_MODULES)}")
    sources = {name: git_state(tree) for name, tree in sorted(trees.items())}
    roots = {var: git_state(os.environ[var]) if os.environ.get(var) else "unset"
             for var in ROOTS}
    for what, state in list(sources.items()) + list(roots.items()):
        problems += checkout_problems(what, state)

    lock = unreadable("flake lock", flake_lock, os.environ.get("ADK_NIX_FLAKE_LOCK"))
    if "unavailable" in lock:
        problems.append(f"flake lock: {lock['unavailable']}")
    rev = os.environ.get("ADK_NIX_FLAKE_REV") or ""
    if not (len(rev) == 40 and all(c in "0123456789abcdef" for c in rev)):
        problems.append(f"flake revision {rev!r} is not a clean commit")
    adk_tools = os.environ.get("ADK_NIX_ADK_TOOLS")
    adk_tools_state = git_state(adk_tools) if adk_tools else "unset"
    if adk_tools_state == "unset":
        problems.append("ADK_NIX_ADK_TOOLS is not set")
    elif adk_tools_state.get("commit") != rev:
        problems.append(f"adk-tools checkout is at {adk_tools_state.get('commit')}, "
                        f"the flake at {rev!r}")

    realised = unreadable("closure", closure, os.environ.get("ADK_NIX_CLOSURE_INFO"))
    if "unavailable" in realised:
        problems.append(f"closure: {realised['unavailable']}")
    if "paths_without_hash" in realised:
        problems.append(f"closure: {len(realised['paths_without_hash'])} paths without a hash")
    closure_paths = [p["path"] for p in realised.get("paths", [])]

    openroad = os.environ.get("OPENROAD") or ""
    tools = {"openroad": dict(path=openroad, realpath=os.path.realpath(openroad) if openroad else None,
                              version=run([openroad, "-version"]) if openroad else None),
             "git": dict(version=run(["git", "--version"])),
             "shell": parent_shell()}
    if not openroad:
        problems.append("OPENROAD is not set")
    else:
        if store_path_of(tools["openroad"]["realpath"]) not in set(closure_paths):
            problems.append(f"OpenROAD at {tools['openroad']['realpath']} is outside the realised closure")
        if tools["openroad"]["version"]["exit"] != 0 or not tools["openroad"]["version"]["stdout"]:
            problems.append("openroad -version did not answer")
    if tools["git"]["version"]["exit"] != 0 or not tools["git"]["version"]["stdout"]:
        problems.append("git --version did not answer")
    tools["klayout"], klayout_problems = klayout_tools(os.environ, closure_paths)
    problems += klayout_problems
    tools["z3"], z3_problems = z3_solver()
    problems += z3_problems
    tools["assembly-lvs"], lvs_problems = assembly_lvs(os.environ)
    problems += lvs_problems

    venv = os.path.realpath(os.environ.get("ADK_NIX_VENV") or "")
    if os.path.realpath(sys.prefix) != venv or sys.prefix == sys.base_prefix:
        problems.append(f"running in {sys.prefix}, not in the shell's venv {venv}")
    if site.ENABLE_USER_SITE:
        problems.append("the user site directory is enabled")
    own_dir = os.path.dirname(os.path.realpath(__file__))
    real_path = [os.path.realpath(p) for p in path_at_start + path_added if p]
    trees_by_module = {mod: os.path.realpath(tree) for name, tree in trees.items()
                       for mod in TREE_MODULES.get(name, ())}
    trees_by_dist = {normalized(dist): os.path.realpath(tree) for name, tree in trees.items()
                     for dist in TREE_DISTS.get(name, ())}
    problems += origin_problems(modules, trees_by_module, closure_paths, venv, own_dir,
                                real_path, found_dists, trees_by_dist)

    record = {
        "format": FORMAT,
        # What this is and is not: a capture of the context, taken from inside
        # the shell. `nix develop` enforces none of these, so this can never
        # stand for a launch record.
        "kind": "context-capture",
        "enforced": {"read_only_inputs": False, "network_isolation": False,
                     "resource_limits": False, "timeout": False, "separate_user": False},
        "captured_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": os.environ.get("ADK_NIX_MODE"),
        "flake": {"rev": rev,
                  "adk_tools_checkout": adk_tools_state,
                  "lock": lock},
        "closure": realised,
        "python": {"version": platform.python_version(),
                   "executable": sys.executable, "prefix": sys.prefix,
                   "base_prefix": sys.base_prefix, "path": path_at_start,
                   "path_added_by_imports": path_added,
                   "distributions": found_dists, "install_tools": install_tools()},
        "cwd": cwd,
        "tools": tools,
        "modules": modules,
        "sources": sources,
        "roots": roots,
        "env": {k: os.environ.get(k) for k in ENV_KEYS},
        "problems": problems,
    }
    # "x" as well as the check above: a record is written once. A second
    # capture into the same place is a mistake in how the run was set up, not
    # something to paper over.
    with open(out, "x") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"adk-nix: context recorded at {out}", file=sys.stderr)
    # Written either way, so the reason is on disk; the shell still refuses,
    # because a context with a hole in it is not evidence.
    for problem in problems:
        print(f"adk-nix: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
