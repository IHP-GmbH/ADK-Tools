#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The co-simulation engine the image ships is the one chiplet-cosim's capability entry names.

The image carries Icarus, cocotb and a python3.13 env as Nix store paths
(staging.pin, "Co-simulation engine"), and COSIM_DEVSHELL points chiplet-cosim
at them through the devshell, a store path of its own that holds three links.
chiplet-cosim admits a gate run only when one entry of its capability table
holds the run's whole context. The image fixes the engine's half of it:
the iverilog, vvp and cocotb VPI library sha256, the cocotb version, the
observer's sha256 and the flags cocotb's runner adds. The rest (the PDK
library files, defines, build arguments, timescale, and the masters a run's
census finds) comes from a run's own inputs and is not checked here; the
sg13g2 cell models are not in the image.

Checked in this order. Every failure is reported, and a check that depends on
one that failed is not run:

  1. the store holds exactly the paths the closure list names (with those of
     any --also-closure list, the image's other engines, and of any --carried
     list, paths the image holds for another use), and the four pins are
     among the closure list's;
  2. COSIM_DEVSHELL resolves to the devshell pin, which holds bin/ and nothing
     else, and bin/ holds exactly three links: python3 into the env, and
     iverilog and vvp into Icarus;
  3. chiplet-cosim's own harness resolution (resolve_harness, which starts
     nothing), given the cocotb version the entry names, finds nothing missing;
  4. the interpreter it resolved is the env's own, and the cocotb it found is
     in the env's own site-packages and resolves into the pinned cocotb;
  5. the engine's half of the context equals the entry's;
  6. started as chiplet-cosim starts it, the interpreter imports that cocotb at
     the entry's version from the env's site-packages and finds its libpython;
  7. a small design compiled by that iverilog and run by that vvp under
     cocotb's Icarus runner passes its one test.

Usage:
    check_cosim_engine.py --entry ID --closure FILE --store DIR \\
        [--also-closure FILE ...] [--carried FILE ...] --iverilog PATH \\
        --cocotb PATH --python PATH --devshell PATH
    check_cosim_engine.py --self-test
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

DEVSHELL_ENV = "COSIM_DEVSHELL"
TOOLS = ("iverilog", "vvp")
LIVE_TIMEOUT_S = 300

SMOKE_V = """\
`timescale 1ns/1ps
module smoke(input clk, input d, output reg q, output nd);
  assign nd = ~d;
  always @(posedge clk) q <= d;
endmodule
"""

SMOKE_TEST = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge


@cocotb.test()
async def flop_and_inverter(dut):
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.d.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    assert dut.q.value == 1
    assert dut.nd.value == 0
"""

SMOKE_DRIVER = """\
import json, shutil, sys
from pathlib import Path
work = Path(sys.argv[1])
sys.path.insert(0, str(work))
from cocotb_tools.runner import get_results, get_runner
runner = get_runner("icarus")
runner.build(sources=[work / "smoke.v"], hdl_toplevel="smoke",
             build_dir=work / "build", always=True)
results = runner.test(test_module="test_smoke", hdl_toplevel="smoke",
                      build_dir=work / "build", test_dir=work)
tests, failed = get_results(results)
print(json.dumps({"tests": tests, "failed": failed,
                  "which": {t: shutil.which(t) for t in ("iverilog", "vvp")}}))
"""

PROBE = """\
import json, sys
import cocotb, find_libpython
print(json.dumps({"executable": sys.executable, "version": cocotb.__version__,
                  "file": cocotb.__file__, "libpython": find_libpython.find_libpython()}))
"""


def under(path, root):
    """Whether ``path`` is ``root`` or inside it, by path parts."""
    path, root = pathlib.PurePath(path), pathlib.PurePath(root)
    return path == root or root in path.parents


def named(names, shown=10):
    """At most ``shown`` of ``names``, and how many more there are."""
    more = len(names) - shown
    return ", ".join(names[:shown]) + (" and %d more" % more if more > 0 else "")


def read_closure(path):
    """The store paths a closure list names: one per line, '#' lines skipped."""
    return [line.strip() for line in pathlib.Path(path).read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def check_store(closure, store, pins, also=(), carried=()):
    """``also`` is the other engines' closure lists: the store holds exactly
    all of them together, and no path is in two. ``carried`` is lists of paths
    the store holds for another use (the die build's tools): they may name a
    path an engine list names too, and the store holds exactly the union."""
    failures = []
    engines = list(closure) + [p for other in also for p in other]
    held_for_use = [p for other in carried for p in other]
    names = {pathlib.PurePath(p).name for p in engines + held_for_use}
    for pin in pins:
        if pin not in closure:
            failures.append("%s is not in the closure list" % pin)
    for which, paths in (("the closure list", engines), ("a carried list", held_for_use)):
        if any(pathlib.PurePath(p).parent != pathlib.PurePath(store) for p in paths):
            failures.append("%s names a path outside %s" % (which, store))
    for which, paths in (("", engines), (" in the carried lists", held_for_use)):
        twice = sorted(pathlib.PurePath(p).name for p in set(paths) if paths.count(p) > 1)
        if twice:
            failures.append("%d path(s) listed twice%s: %s" % (len(twice), which, named(twice)))
    held = set(os.listdir(store)) if os.path.isdir(store) else set()
    missing, extra = sorted(names - held), sorted(held - names)
    if missing:
        failures.append("%s lacks %d listed path(s): %s" % (store, len(missing), named(missing)))
    if extra:
        failures.append("%s holds %d unlisted path(s): %s" % (store, len(extra), named(extra)))
    if carried:
        return failures, ("%d store paths, exactly the %d engine list(s) and %d carried list(s)"
                          % (len(names), 1 + len(also), len(carried)))
    if also:
        return failures, "%d store paths, exactly the %d lists" % (len(engines), 1 + len(also))
    return failures, "%d store paths, exactly the list" % len(closure)


def check_devshell(devshell, pin, python, iverilog):
    """A lock pins the devshell by its store path, so the devshell a run reads
    has to be that path: not a directory of the same links, not another store
    path with them, and not the pin with anything added."""
    if not devshell or not os.path.isdir(devshell):
        return ["$%s is unset or not a directory (%r)" % (DEVSHELL_ENV, devshell)], ""
    real = os.path.realpath(devshell)
    if os.path.islink(pin) or real != os.path.realpath(pin):
        if real == os.path.realpath(os.path.abspath(devshell)) and not os.path.islink(devshell):
            return ["$%s is a plain directory (%s), not a link to the devshell pin %s"
                    % (DEVSHELL_ENV, devshell, pin)], ""
        return ["$%s resolves to %s, not to the devshell pin %s" % (DEVSHELL_ENV, real, pin)], ""
    failures = []
    top = sorted(os.listdir(real))
    if top != ["bin"]:
        failures.append("the devshell holds %s, not bin/ alone" % named(top))
    links = {"python3": python, "iverilog": iverilog, "vvp": iverilog}
    bindir = os.path.join(real, "bin")
    held = sorted(os.listdir(bindir)) if os.path.isdir(bindir) else []
    extra = [name for name in held if name not in links]
    if extra:
        failures.append("bin/ holds %d entry(s) beside its three links: %s" % (len(extra), named(extra)))
    for name, target in links.items():
        path = os.path.join(bindir, name)
        if not os.path.lexists(path):
            failures.append("bin/%s is missing" % name)
        elif not os.path.islink(path):
            failures.append("bin/%s is not a link" % name)
        elif not under(os.path.realpath(path), target):
            failures.append("bin/%s resolves to %s, not into %s" % (name, os.path.realpath(path), target))
    return failures, ("%s -> %s, the pin; bin/ holds exactly python3 -> %s, iverilog and vvp -> %s"
                      % (devshell, real, python, iverilog))


def check_harness(block, findings, python, cocotb, cocotb_version):
    """Checks 3 and 4 on what resolve_harness returned."""
    if findings:
        return ["chiplet-cosim's harness check: %s %s: %s" % (f[2], f[3], f[4])
                for f in findings], ""
    failures = []
    interpreter = pathlib.PurePath(block["python"]["path"])
    if interpreter.parent.parent != pathlib.PurePath(python):
        failures.append("the interpreter %s is not the env's own (%s/bin)" % (interpreter, python))
    site = interpreter.parent.parent / "lib" / ("python" + interpreter.name[len("python"):]) / "site-packages"
    package = block["cocotb"]["path"]
    if not under(package, cocotb):
        failures.append("cocotb in %s resolves to %s, not into %s" % (site, package, cocotb))
    if block["cocotb"]["version"] != cocotb_version:
        failures.append("cocotb %s, the entry names %s" % (block["cocotb"]["version"], cocotb_version))
    return failures, "cocotb %s in %s -> %s" % (block["cocotb"]["version"], site, package)


def engine_half(block, observer, flags):
    """The parts of a run's context that the image's engine fixes."""
    return {"iverilog_sha256": block["tools"]["iverilog"]["sha256"],
            "vvp_sha256": block["tools"]["vvp"]["sha256"],
            "vpi_sha256": block["vpi"]["sha256"],
            "cocotb_version": block["cocotb"]["version"],
            "observer_sha256": observer,
            "runner_flags": flags}


def check_entry(entry, half):
    context = entry["context"]
    failures = ["%s: the image gives %r, entry %s has %r" % (key, value, entry["id"], context[key])
                for key, value in half.items() if context.get(key) != value]
    return failures, "%s: iverilog %s, vvp %s, vpi %s, cocotb %s, observer %s, runner flags %s equal" % (
        entry["id"], half["iverilog_sha256"][:8], half["vvp_sha256"][:8], half["vpi_sha256"][:8],
        half["cocotb_version"], half["observer_sha256"][:8], " ".join(half["runner_flags"] or []))


def run_engine(interpreter, devshell, args, cwd):
    """The env's interpreter, started with only its devshell on PATH."""
    env = {"PATH": os.path.join(devshell, "bin") + ":/usr/bin:/bin", "HOME": cwd, "LANG": "C.UTF-8"}
    proc = subprocess.run([interpreter, *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=LIVE_TIMEOUT_S)
    if proc.returncode != 0:
        return None, "exit %d: %s" % (proc.returncode, (proc.stderr or proc.stdout).strip()[-2000:])
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1]), ""
    except (IndexError, ValueError):
        return None, "no JSON line on stdout: %s" % proc.stdout.strip()[-2000:]


def check_live(block, devshell, python, cocotb, cocotb_version):
    """Checks 6 and 7: the interpreter and the simulator, started."""
    interpreter = block["python"]["path"]
    site = str(pathlib.PurePath(python) / "lib")
    notes = []
    with tempfile.TemporaryDirectory(prefix="cosim-engine-") as work:
        facts, why = run_engine(interpreter, devshell, ["-c", PROBE], work)
        if facts is None:
            return ["the interpreter did not answer the probe: %s" % why], notes
        failures = []
        if facts["executable"] != interpreter:
            failures.append("sys.executable is %s, not %s" % (facts["executable"], interpreter))
        if facts["version"] != cocotb_version:
            failures.append("cocotb.__version__ is %s, the entry names %s" % (facts["version"], cocotb_version))
        if not under(facts["file"], site) or not under(os.path.realpath(facts["file"]), cocotb):
            failures.append("cocotb loads from %s, not from the env's site-packages into %s"
                            % (facts["file"], cocotb))
        if not facts["libpython"] or not os.path.isfile(facts["libpython"]):
            failures.append("find_libpython finds no libpython (%r)" % facts["libpython"])
        if failures:
            return failures, notes
        notes.append("live: %s imports cocotb %s from the env; libpython %s"
                     % (pathlib.PurePath(interpreter).name, facts["version"], facts["libpython"]))
        root = pathlib.Path(work)
        (root / "smoke.v").write_text(SMOKE_V)
        (root / "test_smoke.py").write_text(SMOKE_TEST)
        (root / "drive.py").write_text(SMOKE_DRIVER)
        result, why = run_engine(interpreter, devshell, [str(root / "drive.py"), work], work)
        if result is None:
            return ["the Icarus smoke did not run: %s" % why], notes
        for tool in TOOLS:
            found = result["which"].get(tool)
            if not found or os.path.realpath(found) != block["tools"][tool]["path"]:
                failures.append("the runner found %s at %s, not the devshell's %s"
                                % (tool, found, block["tools"][tool]["path"]))
        if result["tests"] != 1 or result["failed"] != 0:
            failures.append("the Icarus smoke ran %d test(s), %d failed"
                            % (result["tests"], result["failed"]))
        if not failures:
            notes.append("live: Icarus smoke under cocotb's runner, %d test, %d failed"
                         % (result["tests"], result["failed"]))
        return failures, notes


def check(args, cosim, environ, live=True):
    """Every failure, and what passed, in order. ``cosim`` gives chiplet-cosim's
    resolve_harness, entries, observer_sha256 and runner_flags; the self-test
    hands in its own."""
    failures, passed = [], []

    def step(name, result):
        found, note = result
        failures.extend("%s: %s" % (name, f) for f in found)
        if not found and note:
            passed.append("%s: %s" % (name, note))
        return not found

    pins = (args.iverilog, args.cocotb, args.python, args.devshell)
    also = [read_closure(other) for other in (getattr(args, "also_closure", None) or [])]
    carried = [read_closure(other) for other in (getattr(args, "carried", None) or [])]
    step("closure", check_store(read_closure(args.closure), args.store, pins, also, carried))
    devshell = environ.get(DEVSHELL_ENV, "")
    if not step("devshell", check_devshell(devshell, args.devshell, args.python, args.iverilog)):
        return failures, passed
    entries = [e for e in cosim.entries() if e["id"] == args.entry]
    if len(entries) != 1:
        failures.append("entry: the capability table has %d entries with id %s" % (len(entries), args.entry))
        return failures, passed
    entry = entries[0]
    version = entry["context"]["cocotb_version"]
    block, found = cosim.resolve_harness({"devshell_env": DEVSHELL_ENV, "cocotb": version}, environ)
    if not step("harness", check_harness(block, found, args.python, args.cocotb, version)):
        return failures, passed
    half = engine_half(block, cosim.observer_sha256(), cosim.runner_flags(version))
    if not step("entry", check_entry(entry, half)):
        return failures, passed
    passed.append("not checked here: library_sources, defines, explicit_flags, timescale and "
                  "masters, which a run's own inputs give; the sg13g2 cell models are not in "
                  "the image")
    if live:
        found, notes = check_live(block, devshell, args.python, args.cocotb, version)
        failures.extend("live: %s" % f for f in found)
        passed.extend(notes)
    return failures, passed


class ChipletCosim:
    """chiplet-cosim's own API, from the installed package."""

    def __init__(self):
        from chiplet_cosim import cosim_capability, cosim_config
        self.capability, self.config = cosim_capability, cosim_config

    def entries(self):
        table, wrong = self.capability.load_table()
        if wrong is not None:
            raise SystemExit("the capability table cannot be read: %s" % (wrong.detail,))
        return list(table.entries)

    def resolve_harness(self, harness, environ):
        return self.config.resolve_harness(harness, environ)

    def observer_sha256(self):
        return self.capability.observer_sha256()

    def runner_flags(self, version):
        return self.capability.runner_flags(version)


def self_test():
    cases = []

    def expect(name, ok):
        cases.append((name, ok))

    h = lambda c: c * 64
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        store = root / "store"
        names = {"iverilog": "a" * 32 + "-iverilog-13", "cocotb": "b" * 32 + "-python3.13-cocotb-2.0.0",
                 "python": "c" * 32 + "-python3-3.13.9-env", "glibc": "d" * 32 + "-glibc-2.40",
                 "devshell": "g" * 32 + "-cosim-devshell"}
        paths = {k: str(store / v) for k, v in names.items()}
        for p in paths.values():
            os.makedirs(p)
        env, ivl, cb = (pathlib.Path(paths[k]) for k in ("python", "iverilog", "cocotb"))
        (env / "bin").mkdir()
        (env / "bin" / "python3.13").write_text("")
        (ivl / "bin").mkdir()
        for tool in TOOLS:
            (ivl / "bin" / tool).write_text("")
        closure = root / "closure.txt"
        closure.write_text("# header\n" + "".join(sorted(p + "\n" for p in paths.values())))
        def three_links(top):
            (top / "bin").mkdir(parents=True)
            os.symlink(env / "bin" / "python3.13", top / "bin" / "python3")
            for tool in TOOLS:
                os.symlink(ivl / "bin" / tool, top / "bin" / tool)

        pin = pathlib.Path(paths["devshell"])
        three_links(pin)
        devshell = root / "cosim-devshell"
        os.symlink(pin, devshell)
        site = env / "lib" / "python3.13" / "site-packages"
        context = {"iverilog_sha256": h("1"), "vvp_sha256": h("2"), "vpi_sha256": h("3"),
                   "cocotb_version": "2.0.0", "observer_sha256": h("4"),
                   "library_sources": [h("5")], "defines": [],
                   "runner_flags": ["-g2012", "-f", "cmds.f"], "explicit_flags": [],
                   "timescale": ["1ns", "1ps"]}
        entry = {"id": "sg13g2.stdcell.icarus13", "context": context}
        block = {"python": {"path": str(env / "bin" / "python3.13"), "sha256": h("6")},
                 "cocotb": {"version": "2.0.0", "path": str(cb / "lib" / "python3.13"
                                                              / "site-packages" / "cocotb")},
                 "vpi": {"path": "", "sha256": h("3")},
                 "tools": {"iverilog": {"path": str(ivl / "bin" / "iverilog"), "sha256": h("1")},
                           "vvp": {"path": str(ivl / "bin" / "vvp"), "sha256": h("2")}}}

        class Fake:
            def __init__(self, **over):
                self.block, self.findings = json.loads(json.dumps(block)), []
                self.table, self.observer, self.flags = [entry], h("4"), ["-g2012", "-f", "cmds.f"]
                for key, value in over.items():
                    setattr(self, key, value)

            def entries(self):
                return self.table

            def resolve_harness(self, harness, environ):
                assert harness == {"devshell_env": DEVSHELL_ENV, "cocotb": "2.0.0"}, harness
                return self.block, self.findings

            def observer_sha256(self):
                return self.observer

            def runner_flags(self, version):
                return self.flags

        args = argparse.Namespace(entry=entry["id"], closure=str(closure), store=str(store),
                                  iverilog=paths["iverilog"], cocotb=paths["cocotb"],
                                  python=paths["python"], devshell=paths["devshell"])
        environ = {DEVSHELL_ENV: str(devshell)}

        def run(fake=None, a=args, e=environ):
            return check(a, fake or Fake(), e, live=False)

        failures, passed = run()
        expect("the image's engine passes", failures == [] and len(passed) == 5)
        expect("what is not checked is said", any(p.startswith("not checked here") for p in passed))
        expect("site-packages is the env's", any(str(site) in p for p in passed))

        os.makedirs(store / ("e" * 32 + "-stray"))
        failures, _ = run()
        expect("an unlisted store path fails", any("1 unlisted path" in f for f in failures))
        os.rmdir(store / ("e" * 32 + "-stray"))
        closure.write_text("".join(p + "\n" for p in sorted(paths.values()) if "glibc" not in p))
        failures, _ = run()
        expect("a store path left off the list fails", any("unlisted path(s): " + names["glibc"] in f
                                                           for f in failures))
        closure.write_text("".join(p + "\n" for p in sorted(paths.values()) if "cocotb" not in p))
        failures, _ = run()
        expect("a pin left off the list fails",
               any("%s is not in the closure list" % paths["cocotb"] in f for f in failures))
        closure.write_text("".join(sorted(p + "\n" for p in paths.values())))

        other = root / "other.txt"
        ngspice = str(store / ("f" * 32 + "-ngspice-45"))
        other.write_text("# header\n" + ngspice + "\n")
        both = argparse.Namespace(**dict(vars(args), also_closure=[str(other)]))
        failures, _ = run(a=both)
        expect("a path the other list names and the store lacks fails",
               any("lacks 1 listed path(s): " + pathlib.PurePath(ngspice).name in f
                   for f in failures))
        os.makedirs(ngspice)
        failures, passed = run(a=both)
        expect("with its list the store holds exactly both",
               failures == [] and any("exactly the 2 lists" in p for p in passed))
        failures, _ = run()
        expect("without its list that path is unlisted", any("1 unlisted path" in f for f in failures))
        other.write_text(ngspice + "\n" + paths["glibc"] + "\n")
        failures, _ = run(a=both)
        expect("a path in both lists fails", any("listed twice: " + names["glibc"] in f
                                                 for f in failures))
        other.write_text(paths["cocotb"] + "\n")
        closure.write_text("".join(p + "\n" for p in sorted(paths.values()) if "cocotb" not in p))
        failures, _ = run(a=both)
        expect("a pin only in the other list fails",
               any("%s is not in the closure list" % paths["cocotb"] in f for f in failures))
        closure.write_text("".join(sorted(p + "\n" for p in paths.values())))
        other.write_text(ngspice + "\n")

        carried = root / "carried.txt"
        tool = str(store / ("d" * 32 + "-openroad"))
        carried.write_text("# header\n" + tool + "\n" + paths["glibc"] + "\n" + ngspice + "\n")
        held = argparse.Namespace(**dict(vars(both), carried=[str(carried)]))
        failures, _ = run(a=held)
        expect("a carried path the store lacks fails",
               any("lacks 1 listed path(s): " + pathlib.PurePath(tool).name in f
                   for f in failures))
        os.makedirs(tool)
        failures, passed = run(a=held)
        expect("a carried list may name the engines' paths, and the store holds the union",
               failures == [] and any("exactly the 2 engine list(s) and 1 carried list(s)" in p
                                      for p in passed))
        os.makedirs(store / ("e" * 32 + "-stray"))
        failures, _ = run(a=held)
        expect("with a carried list a stray store path still fails",
               any("1 unlisted path" in f for f in failures))
        os.rmdir(store / ("e" * 32 + "-stray"))
        twice = argparse.Namespace(**dict(vars(both), carried=[str(carried), str(carried)]))
        failures, _ = run(a=twice)
        expect("a path in two carried lists fails",
               any("listed twice in the carried lists" in f for f in failures))
        failures, _ = run(a=both)
        expect("without the carried list its own path is unlisted",
               any("1 unlisted path" in f for f in failures))
        carried.write_text(tool + "\n" + str(root / "elsewhere") + "\n")
        failures, _ = run(a=held)
        expect("a carried path outside the store fails naming the carried list",
               any("a carried list names a path outside" in f for f in failures))
        os.rmdir(tool)
        os.rmdir(ngspice)

        failures, _ = run(e={})
        expect("no COSIM_DEVSHELL fails", any("is unset or not a directory" in f for f in failures))
        os.remove(pin / "bin" / "vvp")
        os.symlink(env / "bin" / "python3.13", pin / "bin" / "vvp")
        failures, _ = run()
        expect("a tool linked elsewhere fails", any("bin/vvp resolves to" in f for f in failures))
        os.remove(pin / "bin" / "vvp")
        os.symlink(ivl / "bin" / "vvp", pin / "bin" / "vvp")

        # The devshell itself: the pin, and the pin as built, or nothing passes.
        plain = root / "plain-devshell"
        three_links(plain)
        failures, _ = run(e={DEVSHELL_ENV: str(plain)})
        expect("a plain directory with the three links fails",
               failures == ["devshell: $%s is a plain directory (%s), not a link to the devshell pin %s"
                            % (DEVSHELL_ENV, plain, pin)])
        shutil.rmtree(plain)
        another = store / ("h" * 32 + "-cosim-devshell")
        three_links(another)
        closure.write_text("".join(sorted(p + "\n" for p in list(paths.values()) + [str(another)])))
        elsewhere = root / "elsewhere"
        os.symlink(another, elsewhere)
        failures, _ = run(e={DEVSHELL_ENV: str(elsewhere)})
        expect("a link to another store path with the three links fails",
               failures == ["devshell: $%s resolves to %s, not to the devshell pin %s"
                            % (DEVSHELL_ENV, another, pin)])
        os.remove(elsewhere)
        shutil.rmtree(another)
        closure.write_text("".join(sorted(p + "\n" for p in paths.values())))
        os.symlink(ivl / "bin" / "iverilog", pin / "bin" / "cc")
        failures, _ = run()
        expect("a fourth link fails",
               failures == ["devshell: bin/ holds 1 entry(s) beside its three links: cc"])
        os.remove(pin / "bin" / "cc")
        os.remove(pin / "bin" / "vvp")
        (pin / "bin" / "vvp").write_text("")
        failures, _ = run()
        expect("a tool that is not a link fails", failures == ["devshell: bin/vvp is not a link"])
        os.remove(pin / "bin" / "vvp")
        os.symlink(ivl / "bin" / "vvp", pin / "bin" / "vvp")
        (pin / "lib").mkdir()
        failures, _ = run()
        expect("an entry beside bin/ fails", failures == ["devshell: the devshell holds bin, lib, not bin/ alone"])
        (pin / "lib").rmdir()
        failures, passed = run()
        expect("the pin as built passes again", failures == [] and len(passed) == 5)

        failures, _ = run(Fake(table=[]))
        expect("no entry of that id fails", any("0 entries with id" in f for f in failures))
        failures, _ = run(Fake(findings=[("preflight", "UNSUPPORTED", "harness_missing", "cocotb",
                                          "harness_unsupported: cocotb 2.0.1", "co-sim")]))
        expect("a harness finding fails and stops", len(failures) == 1 and "harness_unsupported" in failures[0])
        moved = Fake()
        moved.block["python"]["path"] = str(cb / "bin" / "python3.13")
        failures, _ = run(moved)
        expect("an interpreter outside the env fails", any("is not the env's own" in f for f in failures))
        moved = Fake()
        moved.block["cocotb"]["path"] = str(env / "lib" / "python3.13" / "site-packages" / "cocotb")
        failures, _ = run(moved)
        expect("cocotb outside the pinned cocotb fails", any("not into" in f for f in failures))
        for key, fake in (("vpi_sha256", Fake(block=dict(block, vpi={"path": "", "sha256": h("9")}))),
                          ("observer_sha256", Fake(observer=h("9"))),
                          ("runner_flags", Fake(flags=None))):
            failures, _ = run(fake)
            expect("a different %s fails" % key, any(f.split(": ", 2)[1].startswith(key) for f in failures))
        other = Fake()
        other.block["tools"]["iverilog"]["sha256"] = h("8")
        failures, _ = run(other)
        expect("another iverilog fails naming both", any("iverilog_sha256: the image gives" in f
                                                         and h("1") in f for f in failures))

    for name, ok in cases:
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    failed = [name for name, ok in cases if not ok]
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("entry", "closure", "store", "iverilog", "cocotb", "python", "devshell"):
        ap.add_argument("--" + name)
    ap.add_argument("--also-closure", action="append", default=[], metavar="FILE",
                    help="another engine's closure list; the store holds exactly all lists")
    ap.add_argument("--carried", action="append", default=[], metavar="FILE",
                    help="store paths held for another use; they may overlap the engine lists")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    missing = [n for n in ("entry", "closure", "store", "iverilog", "cocotb", "python", "devshell")
               if not getattr(args, n)]
    if missing:
        ap.error("missing --%s (unless --self-test)" % ", --".join(missing))
    failures, passed = check(args, ChipletCosim(), os.environ)
    for line in passed:
        print(line)
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("the image's co-simulation engine is the one entry %s was shown on" % args.entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
