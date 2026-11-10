#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Write the tail context record of a devShells.tail evidence shell, after
nix-tail/tail-env.sh has set the tail up and before anything runs, and refuse
(exit 1) when the context is incomplete.

nix/capture_context.py has already recorded the CLI shell. This adds what the
tail runs, asked of the two interpreters themselves rather than of this one:

  - the wrappers: for each of the image's three names, the file PATH resolves
    it to, which must be the one in ADK_NIX_TAIL_WRAPPERS, with its sha256 and
    text; every store path the text names must be in the realised closure;
  - kicad-cli: the file PATH resolves it to, which must be the KiCad fork's
    bin/kicad-cli (tail-env.sh appends that directory to PATH), and where it
    resolves to, which must be in the realised closure;
  - the export interpreter (ADK_NIX_TAIL_EXPORT_PYTHON), run with
    PYTHONVERBOSE=1 to import pcbnew. It must run as that venv, its last import
    path entry must be the KiCad fork's site-packages and no other entry may
    lie in the fork, pcbnew and _pcbnew must load from the fork, and every file
    it loads code from and every shared object it maps must lie in the
    realised closure;
  - the worker (KICAD_CHIPLET_PYTHON), which must be the CLI shell's venv
    interpreter, run with PYTHONVERBOSE=1 to register the SG13G2 PCell library
    from PDK_ROOT the way hyp_to_gds.py does and place one via_stack. It must
    place it (an export that cannot draws rectangles instead) with the library
    loaded from PDK_ROOT, then import PinList from GDS_TO_KICAD_ROOT the way
    the interposer PDK's bump_mirror.py does. It must import it from that
    root's pin_list.py (an export that cannot stops at the first die with pin
    lists) and load code only from the realised closure, the venv, that root,
    and the modules the library preprocesses into a directory of its own under
    the temporary directory;
  - the PDK: PDK_ROOT must be ADK_NIX_SG13G2_PDK, a path of the realised
    closure, and its pdk-source.json must state the lock's inputs, checked as
    nix/capture_build_context.py checks it;
  - the plugin tree (ADK_NIX_PLUGIN_ROOT) and the gds_to_kicad tree
    (GDS_TO_KICAD_ROOT, which must hold pin_list.py): each one's commit and
    whether it is clean and the top of its checkout; in evidence mode each
    must also be its tools/ path of ADK_NIX_ADK_TOOLS at that path's gitlink;
  - the variables the tail hook clears (TAIL_SCRUB, and any named KLAYOUT in
    any case): none may be set.

A capture, not a qualification: it shows what the two interpreters load when
asked, not what an export later does with them.

Usage:
    capture_tail_context.py <out.json>
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

_LIB = os.environ.get("ADK_NIX_LIB") or ""
if not os.path.isfile(os.path.join(_LIB, "capture_build_context.py")):
    sys.exit(f"adk-nix: tail context: ADK_NIX_LIB={_LIB!r} holds no capture_build_context.py")
sys.path.insert(0, _LIB)
import capture_context as cc  # noqa: E402
import capture_build_context as cbc  # noqa: E402

FORMAT = "adk-tools-nix-tail-context/v0"
COMMANDS = ("chiplet-export", "adk-verify-carrier-drc", "adk-drc")
KICAD_CLI = "kicad-cli"
PLUGIN_PATH = "tools/chiplet_kicad_plugin"
G2K_PATH = "tools/gds_to_kicad"
G2K_FILE = "pin_list.py"
# nix-tail/tail-env.sh's ADK_NIX_TAIL_SCRUB (the tests hold the two equal).
TAIL_SCRUB = ("KLAYOUT_LYP_FILE", "NIX_PYTHONPATH",
              "IHP_PYCELL_LIB_PRINT_PROCESS_TREE", "IHP_PYCELL_LIB_PRINT_DEFINES_SET")
PCELL_LIB = "ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib"
PROBE_TIMEOUT = 120
STORE_REF = re.compile(r"/nix/store/[0-9a-z]{32}-[^/\s'\"]+")
# The directory sg13g2_pycell_lib preprocesses its '#ifdef' modules into
# (tempfile.mkdtemp(prefix='sg13g2_pycell_')), and the files it writes there.
PREPROCESSED = re.compile(r"sg13g2_pycell_[^/]+/[A-Za-z0-9_]+_pre\.py")

# Run by the export interpreter as chiplet-export's own import of pcbnew would
# run, with -P so that no working directory comes first on the path.
EXPORT_PROBE = r"""
import json, sys
path = list(sys.path)
import pcbnew
core = sys.modules.get('_pcbnew')
maps = set()
with open('/proc/self/maps') as handle:
    for line in handle:
        parts = line.split(None, 5)
        if len(parts) == 6 and parts[5].startswith('/'):
            maps.add(parts[5].rstrip('\n'))
print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix,
                  'base_prefix': sys.base_prefix, 'path': path,
                  'version_info': list(sys.version_info[:2]),
                  'pcbnew': pcbnew.__file__, 'core': getattr(core, '__file__', None),
                  'build_version': pcbnew.GetBuildVersion(), 'maps': sorted(maps)}))
"""

# Run by the worker: hyp_to_gds.py's bootstrap of the SG13G2 PCell library and
# its test via_stack, in the same order and with the same parameters, then
# bump_mirror.py's import of PinList from the root this shell sets (its first
# candidate; the walk up from its own file that follows is never reached).
WORKER_PROBE = r"""
import json, os, sys, tempfile
import klayout.db as db
tech_dir = os.path.join(os.environ['PDK_ROOT'], 'ihp-sg13g2', 'libs.tech', 'klayout')
python_dir = os.path.join(tech_dir, 'python')
cni_dir = os.path.join(python_dir, 'pycell4klayout-api', 'source', 'python')
for entry in (python_dir, cni_dir):
    if entry not in sys.path:
        sys.path.insert(0, entry)
error = None
try:
    if 'sg13g2' not in db.Technology.technology_names():
        tech = db.Technology.create_technology('sg13g2')
        lyt = os.path.join(tech_dir, 'tech', 'sg13g2.lyt')
        if os.path.isfile(lyt):
            tech.load(lyt)
    if 'SG13_dev' not in db.Library.library_names():
        import sg13g2_pycell_lib
except (Exception, SystemExit) as exc:
    error = '%s: %s' % (type(exc).__name__, exc)
cell = None
if error is None:
    layout = db.Layout()
    layout.technology_name = 'sg13g2'
    try:
        cell = layout.create_cell('via_stack', 'SG13_dev', {
            'b_layer': 'Metal4', 't_layer': 'Metal5', 'vn_columns': 1, 'vn_rows': 1})
    except Exception as exc:
        error = '%s: %s' % (type(exc).__name__, exc)
lib = sys.modules.get('sg13g2_pycell_lib')
pin_list = pin_list_error = None
try:
    import importlib.util
    root = os.environ['GDS_TO_KICAD_ROOT']
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location('pin_list', os.path.join(root, 'pin_list.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules['pin_list'] = module
    spec.loader.exec_module(module)
    module.PinList
    pin_list = module.__file__
except (Exception, SystemExit) as exc:
    pin_list_error = '%s: %s' % (type(exc).__name__, exc)
print(json.dumps({'executable': sys.executable, 'db': db.__file__,
                  'pycell_lib': getattr(lib, '__file__', None),
                  'libraries': sorted(db.Library.library_names()),
                  'via_stack': cell is not None,
                  'bbox': cell.bbox().to_s() if cell is not None else None,
                  'pin_list': pin_list, 'pin_list_error': pin_list_error,
                  'tmp': tempfile.gettempdir(), 'error': error}))
"""


def shared(path):
    base = os.path.basename(path)
    return base.endswith(".so") or ".so." in base or base.endswith(".kiface")


def in_any(path, roots):
    return any(cc.inside(path, root) for root in roots if root)


def probe(argv, env):
    """Run one probe with PYTHONVERBOSE=1: {argv, exit, reply, stdout_other,
    stderr_other, stderr_sha256, files, other_loaders, unread_trace}, reply
    being the JSON object on the last line of stdout, or None. Never raises."""
    record = {"argv": list(argv), "env": {"PYTHONVERBOSE": "1"}}
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT, env=dict(env, PYTHONVERBOSE="1"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        record.update(exit=None, reply=None, error=f"{exc.__class__.__name__}: {exc}")
        return record
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    reply = None
    if lines:
        try:
            reply = json.loads(lines[-1])
        except ValueError:
            reply = None
    files, loaders, unread, rest = cbc.parse_trace(done.stderr)
    record.update(exit=done.returncode,
                  reply=reply if isinstance(reply, dict) else None,
                  stdout_other=lines[:-1] if lines else [],
                  stderr_other=rest,
                  stderr_sha256=hashlib.sha256(done.stderr.encode()).hexdigest(),
                  files=sorted({os.path.realpath(f) for f in files}),
                  other_loaders=loaders, unread_trace=unread)
    return record


def trace_problems(what, record, allowed):
    """Loads the trace could not account for, and any file loaded from
    outside `allowed` (a predicate on a realpath)."""
    problems = [f"{what}: loaded by a loader that names no file: {line}"
                for line in record.get("other_loaders", [])]
    problems += [f"{what}: a trace line that names a load could not be read: {line}"
                 for line in record.get("unread_trace", [])]
    problems += [f"{what}: {f} loaded from outside the realised closure"
                 for f in record.get("files", []) if not allowed(f)]
    return problems


def wrappers_record(wrappers, env, closure_paths, problems, which=shutil.which):
    """{name: {path, resolved, sha256, text, store_paths}} for the three
    commands, as PATH resolves them."""
    record = {}
    for name in COMMANDS:
        want = os.path.join(wrappers, "bin", name) if wrappers else None
        found = which(name, path=env.get("PATH", ""))
        entry = {"path": found, "expected": want}
        record[name] = entry
        if not want or found != want:
            problems.append(f"wrapper: {name} resolves to {found}, not {want}")
            continue
        try:
            with open(found, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            problems.append(f"wrapper: {found} unreadable: {exc.strerror or exc}")
            continue
        text = raw.decode("utf-8", errors="replace")
        refs = sorted(set(STORE_REF.findall(text)))
        entry.update(resolved=os.path.realpath(found), sha256=hashlib.sha256(raw).hexdigest(),
                     text=text, store_paths=refs)
        for ref in refs:
            if ref not in closure_paths:
                problems.append(f"wrapper: {name} names {ref}, not a path of the realised closure")
    return record


def kicad_cli_record(fork, env, closure_paths, problems, which=shutil.which):
    """{path, expected, resolved} for kicad-cli, as PATH resolves it."""
    want = os.path.join(fork, "bin", KICAD_CLI) if fork else None
    found = which(KICAD_CLI, path=env.get("PATH", ""))
    record = {"path": found, "expected": want}
    if not want or found != want:
        problems.append(f"kicad-cli resolves to {found}, not {want}")
        return record
    record["resolved"] = os.path.realpath(found)
    if not in_any(record["resolved"], closure_paths):
        problems.append(f"kicad-cli: {record['resolved']} is outside the realised closure")
    return record


def export_problems(record, export, fork, closure_paths):
    """What is wrong with the export interpreter's probe."""
    what = "export interpreter"
    reply = record.get("reply")
    if record.get("exit") != 0 or reply is None:
        return [f"{what}: the pcbnew probe exited {record.get('exit')} "
                f"({record.get('error') or 'no reply'})"]
    problems = []
    if os.path.realpath(reply.get("prefix") or "") != os.path.realpath(export):
        problems.append(f"{what}: runs as {reply.get('prefix')!r}, not the venv {export}")
    major, minor = (reply.get("version_info") or [None, None])[:2]
    site = os.path.join(fork, "lib", f"python{major}.{minor}", "site-packages")
    path = [os.path.realpath(p) for p in reply.get("path") or []]
    if not path or path[-1] != os.path.realpath(site):
        problems.append(f"{what}: the last import path entry is "
                        f"{path[-1] if path else None!r}, not the fork's {site}")
    elif any(cc.inside(p, fork) for p in path[:-1]):
        problems.append(f"{what}: the fork is on the import path before its site-packages")
    for key in ("pcbnew", "core"):
        real = os.path.realpath(reply.get(key) or "")
        if not reply.get(key) or not cc.inside(real, fork):
            problems.append(f"{what}: {key} loaded from {reply.get(key)!r}, not from {fork}")
    for mapped in reply.get("maps") or []:
        mapped = mapped.replace(" (deleted)", "")
        if shared(mapped) and not in_any(mapped, closure_paths):
            problems.append(f"{what}: {mapped} mapped from outside the realised closure")
    problems += trace_problems(what, record, lambda f: in_any(f, closure_paths))
    return problems


def worker_problems(record, venv, pdk, g2k, closure_paths):
    """What is wrong with the worker's probe; g2k is GDS_TO_KICAD_ROOT."""
    what = "worker"
    reply = record.get("reply")
    if record.get("exit") != 0 or reply is None:
        return [f"{what}: the PCell probe exited {record.get('exit')} "
                f"({record.get('error') or 'no reply'})"]
    problems = []
    if reply.get("error") or not reply.get("via_stack"):
        problems.append(f"{what}: no via_stack placed from the SG13G2 PCell library "
                        f"({reply.get('error') or 'create_cell returned nothing'}); "
                        f"an export here would draw rectangles instead")
    lib = reply.get("pycell_lib")
    if not lib or not cc.inside(os.path.realpath(lib), os.path.join(pdk, PCELL_LIB)):
        problems.append(f"{what}: sg13g2_pycell_lib loaded from {lib!r}, not from {pdk}")
    g2k_real = os.path.realpath(g2k) if g2k else None
    source = reply.get("pin_list")
    if (reply.get("pin_list_error") or not source or not g2k_real
            or os.path.realpath(source) != os.path.join(g2k_real, G2K_FILE)):
        problems.append(f"{what}: PinList not imported from {g2k!r}/{G2K_FILE} "
                        f"({reply.get('pin_list_error') or source!r}); an export here "
                        f"stops at the first die with pin lists")
    tmp = os.path.realpath(reply.get("tmp") or "/nonexistent")

    def allowed(f):
        if in_any(f, closure_paths) or cc.inside(f, os.path.realpath(venv)):
            return True
        if g2k_real and cc.inside(f, g2k_real):
            return True
        return cc.inside(f, tmp) and bool(PREPROCESSED.fullmatch(os.path.relpath(f, tmp)))
    problems += trace_problems(what, record, allowed)
    return problems


def tree_record(what, path, root, mode, adk_tools, problems):
    """A root the tail hook set from a tools/ submodule: its state, and in
    evidence mode its gitlink, which it must be at."""
    state = cc.git_state(root) if root else None
    problems += cc.checkout_problems(what, state)
    record = {"root": root, "state": state}
    if mode == "evidence":
        want = os.path.join(adk_tools or "", path)
        link = cc.run(["git", "-C", adk_tools or "", "ls-tree", "HEAD", path])
        parts = link["stdout"].split() if link["exit"] == 0 else []
        gitlink = parts[2] if len(parts) == 4 and parts[1] == "commit" else None
        record.update(expected_root=want, gitlink=gitlink)
        if os.path.realpath(root or "") != os.path.realpath(want):
            problems.append(f"{what}: {root} is not {want}")
        if not gitlink or (state or {}).get("commit") != gitlink:
            problems.append(f"{what}: at {(state or {}).get('commit')}, "
                            f"the gitlink is {gitlink}")
    return record


def plugin_record(plugin, mode, adk_tools, problems):
    return tree_record("chiplet_kicad_plugin", PLUGIN_PATH, plugin, mode, adk_tools, problems)


def gds_to_kicad_record(root, mode, adk_tools, problems):
    record = tree_record("gds_to_kicad", G2K_PATH, root, mode, adk_tools, problems)
    if not root or not os.path.isfile(os.path.join(root, G2K_FILE)):
        problems.append(f"gds_to_kicad: no {G2K_FILE} under {root!r}")
    return record


def scrub_problems(env):
    """A variable the tail hook clears that is set again."""
    return [f"{name} is set; the tail hook clears it" for name in sorted(env)
            if name in TAIL_SCRUB or name.lower() == "klayout"]


def pdk_record(pdk, lock, problems):
    """The PDK's inputs in the flake lock, its pdk-source.json and what that
    statement says otherwise than the two, read as the build shell's capture
    reads them. Appends to problems."""
    locked = cc.unreadable("flake lock", lambda p: cbc.lock_inputs(p, cbc.LOCK_INPUTS["pdk"]),
                           lock)
    if "unavailable" in locked:
        problems.append(f"pdk: {locked['unavailable']}")
    source, statement, parsed = cbc.read_pdk_source(pdk, problems)
    if parsed:
        problems += cbc.statement_problems(statement, pdk, locked)
    return {"lock": locked, "source": source, "statement": statement}


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = argv[1]
    if os.path.lexists(out):
        print(f"adk-nix: {out} exists; a tail context record is written once", file=sys.stderr)
        return 1
    env = dict(os.environ)
    problems = []
    names = ("ADK_NIX_TAIL_WRAPPERS", "ADK_NIX_TAIL_EXPORT_PYTHON", "ADK_NIX_KICAD_FORK_BASE",
             "ADK_NIX_SG13G2_PDK", "ADK_NIX_VENV", "ADK_NIX_PLUGIN_ROOT", "ADK_NIX_MODE")
    for name in names:
        if not env.get(name):
            problems.append(f"{name} is not set")
    wrappers, export, fork, pdk, venv, plugin, mode = (env.get(n) or "" for n in names)
    if not pdk or env.get("PDK_ROOT") != pdk:
        problems.append(f"PDK_ROOT={env.get('PDK_ROOT')!r}, expected {pdk!r}")
    worker = env.get("KICAD_CHIPLET_PYTHON")
    if not venv or worker != os.path.join(venv, "bin", "python"):
        problems.append(f"KICAD_CHIPLET_PYTHON={worker!r}, expected the venv's {venv}/bin/python")
    g2k = env.get("GDS_TO_KICAD_ROOT") or ""
    problems += scrub_problems(env)

    realised = cc.unreadable("closure", cc.closure, env.get("ADK_NIX_CLOSURE_INFO"))
    if "unavailable" in realised:
        problems.append(f"closure: {realised['unavailable']}")
    by_path = {p["path"]: p for p in realised.get("paths", [])}
    closure_paths = list(by_path)
    stores = {}
    for what, path in (("wrappers", wrappers), ("export_python", export),
                       ("kicad_fork_base", fork), ("pdk", pdk)):
        entry = by_path.get(path) if path else None
        if entry is None:
            problems.append(f"{what} {path!r} is not a path of the realised closure")
        elif not cbc.NAR_HASH.fullmatch(str(entry.get("narHash", ""))):
            problems.append(f"{what} {path!r} has no narHash in the realised closure")
        stores[what] = {"store_path": path, "closure": entry}

    record_wrappers = wrappers_record(wrappers, env, closure_paths, problems)
    record_kicad_cli = kicad_cli_record(fork, env, closure_paths, problems)

    export_probe = None
    if export and fork:
        export_probe = probe([os.path.join(export, "bin", "python"), "-P", "-c", EXPORT_PROBE],
                             env)
        problems += export_problems(export_probe, export, fork, closure_paths)
    else:
        problems.append("export probe not run: no export interpreter or fork")
    worker_probe = None
    if worker and venv and pdk:
        worker_probe = probe([worker, "-P", "-c", WORKER_PROBE], env)
        problems += worker_problems(worker_probe, venv, pdk, g2k, closure_paths)
    else:
        problems.append("worker probe not run: no worker, venv or PDK")

    stores["pdk"].update(pdk_record(pdk, env.get("ADK_NIX_FLAKE_LOCK"), problems))

    record = {"format": FORMAT, "kind": "context-capture", "problems": problems,
              "wrappers": record_wrappers, "kicad_cli": record_kicad_cli, "stores": stores,
              "export_probe": export_probe, "worker": worker, "worker_probe": worker_probe,
              "plugin": plugin_record(plugin, mode, env.get("ADK_NIX_ADK_TOOLS"), problems),
              "gds_to_kicad": gds_to_kicad_record(g2k, mode, env.get("ADK_NIX_ADK_TOOLS"),
                                                  problems),
              "closure": {k: realised.get(k) for k in
                          ("closure_info", "registration_sha256", "unavailable")},
              "note": "a capture of what the export interpreter and the worker load when "
                      "asked; not a qualification of any export"}
    with open(out, "x") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    for problem in problems:
        print(f"adk-nix: tail context: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
