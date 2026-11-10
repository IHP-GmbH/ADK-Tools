#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The mixed-signal engine the image ships is the one staging.pin names, and it runs.

The image carries ngspice 45, an Icarus built with libvvp and the six sg13g2
OSDI models as Nix store paths (staging.pin, "Mixed-signal engine"), and
MIXSIM_DEVSHELL links into them. The OSDI files are not reproducible from their
recipe by every compiler run, so their identity is their bytes, which
nix/sg13g2-osdi.sha256 names.

Checked in this order. Every failure is reported, and a check that depends on
one that failed is not run:

  1. the closure list holds the three pins, and the store holds every path it
     lists (the co-simulation leg holds the store to all lists together);
  2. MIXSIM_DEVSHELL's bin/ngspice, bin/iverilog, bin/vvp, lib/libvvp.so,
     lib/ngspice and osdi resolve to those paths inside the pins;
  3. its osdi holds exactly the files the sha256 list names, by their bytes;
  4. live, ngspice: a batch deck that loads every OSDI file with pre_osdi,
     before the circuit is parsed, and solves an operating point on a resistor
     exits 0, prints no error and solves. Its ablation loads a file that does
     not exist and must print ngspice's open error, which keeps exit code 0, so
     the text is what is decided on. A third run lists the devices, kept as a
     reading;
  5. live, Icarus under d_cosim: a one-bit output that rises at 10 us, compiled
     by bin/iverilog, runs through lib/ngspice's ivlng shim given lib/libvvp.so
     and the VPI module by absolute path, against a libvvp.so on
     LD_LIBRARY_PATH that is a shared library but not Icarus's. With explicit
     paths and without that variable, and with it, the output must read 1; with
     libvvp left to the name search under it, the shim must name that very file
     as lacking VVP functions, which is what the explicit path keeps out.

Usage:
    check_mixsim_engine.py --closure FILE --store DIR --osdi-sha256 FILE \\
        --ngspice PATH --iverilog PATH --osdi PATH [--cc CC]
    check_mixsim_engine.py --self-test
"""

import argparse
import hashlib
import os
import pathlib
import re
import subprocess
import sys
import tempfile

DEVSHELL_ENV = "MIXSIM_DEVSHELL"
LIVE_TIMEOUT_S = 300
# (in MIXSIM_DEVSHELL, pin, inside the pin)
LINKS = (("bin/ngspice", "ngspice", "bin/ngspice"),
         ("bin/iverilog", "iverilog", "bin/iverilog"),
         ("bin/vvp", "iverilog", "bin/vvp"),
         ("lib/libvvp.so", "iverilog", "lib/libvvp.so"),
         ("lib/ngspice", "ngspice", "lib/ngspice"),
         ("osdi", "osdi", "osdi"))

TICK_V = """\
`timescale 1ns/1ps
module tick(output reg q);
  initial begin
    q = 1'b0;
    #10000 q = 1'b1;
  end
endmodule
"""
NOT_VVP_C = "int adk_check_not_vvp;\n"
ABSENT_OSDI = "/adk-check-absent.osdi"
OSDI_DEVICE = re.compile(r"^(\S+)\s*:\s*A simulator independent device loaded with OSDI", re.M)
EPRINT_ONE = re.compile(r"^[0-9.eE+-]+\s+1[a-z]?\s*$", re.M)


def named(names, shown=10):
    """At most ``shown`` of ``names``, and how many more there are."""
    more = len(names) - shown
    return ", ".join(names[:shown]) + (" and %d more" % more if more > 0 else "")


def read_lines(path):
    """A list file's lines: '#' lines and blank ones skipped."""
    return [line.strip() for line in pathlib.Path(path).read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_store(closure, store, pins):
    failures = []
    for pin in pins:
        if pin not in closure:
            failures.append("%s is not in the closure list" % pin)
    if any(pathlib.PurePath(p).parent != pathlib.PurePath(store) for p in closure):
        failures.append("the closure list names a path outside %s" % store)
    held = set(os.listdir(store)) if os.path.isdir(store) else set()
    missing = sorted(n for n in (pathlib.PurePath(p).name for p in closure) if n not in held)
    if missing:
        failures.append("%s lacks %d listed path(s): %s" % (store, len(missing), named(missing)))
    return failures, "%d listed store paths present" % len(closure)


def check_devshell(devshell, pins):
    if not devshell or not os.path.isdir(devshell):
        return ["$%s is unset or not a directory (%r)" % (DEVSHELL_ENV, devshell)], ""
    failures = []
    for name, pin, inner in LINKS:
        link = os.path.join(devshell, name)
        want = os.path.join(pins[pin], inner)
        real = os.path.realpath(link)
        if not os.path.exists(link):
            failures.append("%s does not resolve to anything" % name)
        elif real != os.path.realpath(want):
            failures.append("%s resolves to %s, not %s" % (name, real, want))
    return failures, "%s: %d links into the three pins" % (devshell, len(LINKS))


def check_osdi(osdi_dir, sha_list):
    want = read_lines(sha_list)
    names = sorted(n for n in os.listdir(osdi_dir) if n.endswith(".osdi")) \
        if os.path.isdir(osdi_dir) else []
    got = ["%s  %s" % (sha256(os.path.join(osdi_dir, n)), n) for n in names]
    if not want:
        return ["%s names no file" % sha_list], ""
    failures = []
    listed = {line.split("  ", 1)[1]: line for line in want}
    found = dict(zip(names, got))
    for name in sorted(set(listed) - set(found)):
        failures.append("%s is listed and not in %s" % (name, osdi_dir))
    for name in sorted(set(found) - set(listed)):
        failures.append("%s is in %s and not listed" % (name, osdi_dir))
    for name in sorted(set(found) & set(listed)):
        if found[name] != listed[name]:
            failures.append("%s has sha256 %s, the list names %s"
                            % (name, found[name].split()[0], listed[name].split()[0]))
    return failures, "%d OSDI files, the bytes %s names" % (len(want), os.path.basename(sha_list))


def osdi_deck(files, devhelp=False):
    return "".join(["* OSDI load\n", "V1 1 0 1\n", "R1 1 0 1k\n", ".control\n",
                    *("pre_osdi '%s'\n" % f for f in files), "op\n",
                    "devhelp\n" if devhelp else "", ".endc\n", ".end\n"])


def cosim_deck(devshell, lib_args):
    return "".join([
        "* d_cosim with Icarus\n",
        "adut null [ q ] tick\n",
        '.model tick d_cosim simulation="%s/lib/ngspice/ivlng.so"\n' % devshell,
        '+ sim_args=["tick.vvp"] lib_args=[%s]\n' % lib_args,
        ".control\n", "tran 1u 30u\n", "eprint q\n", ".endc\n", ".end\n"])


def decide_osdi(load_rc, load, ablation):
    failures = []
    if load_rc != 0:
        failures.append("the OSDI load run exited %d" % load_rc)
    if re.search("error", load, re.I):
        failures.append("the OSDI load run printed an error")
    if "No. of Data Rows" not in load:
        failures.append("the OSDI load run solved nothing")
    if "Error opening osdi lib" not in ablation:
        failures.append("the ablation (an absent OSDI file) printed no open error, "
                        "so the load run's silence proves nothing")
    return failures


def ran(rc, out):
    """A d_cosim run that ran, and printed the output at 1."""
    return rc == 0 and not re.search("cannot open|failed", out, re.I) \
        and EPRINT_ONE.search(out) is not None


def decide_cosim(clean, poisoned, by_name, poison):
    failures = []
    if not ran(*clean):
        failures.append("d_cosim with explicit paths did not run to q=1 (exit %d)" % clean[0])
    if not ran(*poisoned):
        failures.append("d_cosim with explicit paths and the poison on LD_LIBRARY_PATH "
                        "did not run to q=1 (exit %d)" % poisoned[0])
    if not re.search(r"Icarus shim failed to find VVP function: %s\b" % re.escape(poison),
                     by_name[1]):
        failures.append("the by-name run did not name %s as lacking VVP functions, so "
                        "the explicit runs' passing shows nothing" % poison)
    return failures


def run(argv, cwd, env):
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors="replace",
                           timeout=LIVE_TIMEOUT_S)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "%s: %s" % (argv[0], e)


def check_live(devshell, cc, work):
    """Failures, notes, and the runs' output to show when anything failed."""
    failures, notes = [], []
    ngspice = os.path.join(devshell, "bin", "ngspice")
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    osdi = os.path.join(devshell, "osdi")
    files = [os.path.join(osdi, n) for n in sorted(os.listdir(osdi)) if n.endswith(".osdi")]
    decks = {"load": osdi_deck(files), "ablation": osdi_deck([ABSENT_OSDI]),
             "devhelp": osdi_deck(files, devhelp=True)}
    out = {}
    for name, deck in decks.items():
        (work / ("%s.cir" % name)).write_text(deck)
        out[name] = run([ngspice, "-b", "-n", "%s.cir" % name], work, env)
    found = decide_osdi(out["load"][0], out["load"][1], out["ablation"][1])
    failures.extend("ngspice: %s" % f for f in found)
    if not found:
        devices = OSDI_DEVICE.findall(out["devhelp"][1])
        notes.append("live: ngspice loaded %d OSDI files with pre_osdi and solved; the "
                     "ablation printed the open error; devhelp lists %s"
                     % (len(files), ", ".join(devices) or "no OSDI device"))

    (work / "tick.v").write_text(TICK_V)
    rc, text = run([os.path.join(devshell, "bin", "iverilog"), "-o", "tick.vvp", "tick.v"],
                   work, env)
    if rc != 0:
        return failures + ["d_cosim: iverilog could not compile the tick (exit %d): %s"
                           % (rc, text.strip())], notes, out
    (work / "poison").mkdir()
    (work / "not-vvp.c").write_text(NOT_VVP_C)
    poison = work / "poison" / "libvvp.so"
    rc, text = run([cc, "-shared", "-fPIC", "-o", str(poison), "not-vvp.c"], work, env)
    if rc != 0:
        return failures + ["d_cosim: %s could not build the poison library (exit %d): %s"
                           % (cc, rc, text.strip())], notes, out
    shim = "%s/lib/ngspice/ivlng" % devshell
    (work / "explicit.cir").write_text(
        cosim_deck(devshell, '"%s/lib/libvvp.so" "%s"' % (devshell, shim)))
    (work / "by-name.cir").write_text(cosim_deck(devshell, '"libvvp" "%s"' % shim))
    poisoned_env = dict(env, LD_LIBRARY_PATH=str(work / "poison"))
    clean = run([ngspice, "-b", "explicit.cir"], work, env)
    poisoned = run([ngspice, "-b", "explicit.cir"], work, poisoned_env)
    by_name = run([ngspice, "-b", "by-name.cir"], work, poisoned_env)
    found = decide_cosim(clean, poisoned, by_name, str(poison))
    failures.extend("d_cosim: %s" % f for f in found)
    if not found:
        notes.append("live: d_cosim ran Icarus to q=1 with explicit paths, with and without "
                     "a non-VVP libvvp.so on LD_LIBRARY_PATH; by name it took that file "
                     "(exit %d)" % by_name[0])
    out.update({"clean": clean, "poisoned": poisoned, "by-name": by_name})
    return failures, notes, out


def check(args, environ, live=True):
    """Every failure, and what passed, in order."""
    failures, passed = [], []

    def step(name, result):
        found, note = result
        failures.extend("%s: %s" % (name, f) for f in found)
        if not found and note:
            passed.append("%s: %s" % (name, note))
        return not found

    pins = {"ngspice": args.ngspice, "iverilog": args.iverilog, "osdi": args.osdi}
    if not step("closure", check_store(read_lines(args.closure), args.store, list(pins.values()))):
        return failures, passed
    devshell = environ.get(DEVSHELL_ENV, "")
    if not step("devshell", check_devshell(devshell, pins)):
        return failures, passed
    if not step("osdi", check_osdi(os.path.join(devshell, "osdi"), args.osdi_sha256)):
        return failures, passed
    if live:
        with tempfile.TemporaryDirectory() as tmp:
            found, notes, out = check_live(devshell, args.cc, pathlib.Path(tmp))
        failures.extend("live: %s" % f for f in found)
        passed.extend(notes)
        if found:
            for name, (rc, text) in out.items():
                failures.append("live: the %s run (exit %d) printed:\n%s" % (name, rc, text.rstrip()))
    return failures, passed


def self_test():
    cases = []

    def expect(name, ok):
        cases.append((name, ok))

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        store = root / "store"
        names = {"ngspice": "a" * 32 + "-ngspice-45",
                 "iverilog": "b" * 32 + "-iverilog-libvvp-s20250103",
                 "osdi": "c" * 32 + "-sg13g2-osdi-22f4335", "fftw": "d" * 32 + "-fftw-3.3.10"}
        paths = {k: str(store / v) for k, v in names.items()}
        for p in paths.values():
            os.makedirs(p)
        ng, ivl, od = (pathlib.Path(paths[k]) for k in ("ngspice", "iverilog", "osdi"))
        for d in (ng / "bin", ng / "lib" / "ngspice", ivl / "bin", ivl / "lib", od / "osdi"):
            d.mkdir(parents=True)
        for f in (ng / "bin" / "ngspice", ivl / "bin" / "iverilog", ivl / "bin" / "vvp",
                  ivl / "lib" / "libvvp.so"):
            f.write_text("")
        (od / "osdi" / "a.osdi").write_bytes(b"a")
        (od / "osdi" / "b.osdi").write_bytes(b"b")
        sha_list = root / "osdi.sha256"
        sha_list.write_text("# header\n" + "".join(
            "%s  %s\n" % (hashlib.sha256(c).hexdigest(), n) for n, c in (("a.osdi", b"a"),
                                                                       ("b.osdi", b"b"))))
        closure = root / "closure.txt"
        closure.write_text("# header\n" + "".join(sorted(p + "\n" for p in paths.values())))
        devshell = root / "devshell"
        (devshell / "bin").mkdir(parents=True)
        (devshell / "lib").mkdir()
        for name, pin, inner in LINKS:
            os.symlink(os.path.join(paths[pin], inner), devshell / name)
        args = argparse.Namespace(closure=str(closure), store=str(store),
                                  osdi_sha256=str(sha_list), ngspice=paths["ngspice"],
                                  iverilog=paths["iverilog"], osdi=paths["osdi"], cc="cc")
        environ = {DEVSHELL_ENV: str(devshell)}

        def go(a=args, e=environ):
            return check(a, e, live=False)

        failures, passed = go()
        expect("the image's engine passes", failures == [] and len(passed) == 3)

        closure.write_text("".join(p + "\n" for p in sorted(paths.values()) if "osdi" not in p))
        failures, _ = go()
        expect("a pin left off the list fails",
               any("%s is not in the closure list" % paths["osdi"] in f for f in failures))
        closure.write_text("".join(sorted(p + "\n" for p in paths.values())))
        os.rmdir(paths["fftw"])
        failures, _ = go()
        expect("a listed path the store lacks fails",
               any("lacks 1 listed path(s): " + names["fftw"] in f for f in failures))
        os.makedirs(paths["fftw"])

        failures, _ = go(e={})
        expect("no MIXSIM_DEVSHELL fails", any("is unset or not a directory" in f for f in failures))
        os.remove(devshell / "lib" / "libvvp.so")
        os.symlink(ng / "bin" / "ngspice", devshell / "lib" / "libvvp.so")
        failures, _ = go()
        expect("a link into the wrong file fails",
               any("lib/libvvp.so resolves to" in f for f in failures))
        os.remove(devshell / "lib" / "libvvp.so")
        os.symlink(ivl / "lib" / "absent.so", devshell / "lib" / "libvvp.so")
        failures, _ = go()
        expect("a dangling link fails", any("lib/libvvp.so does not resolve" in f for f in failures))
        os.remove(devshell / "lib" / "libvvp.so")
        os.symlink(ivl / "lib" / "libvvp.so", devshell / "lib" / "libvvp.so")

        (od / "osdi" / "b.osdi").write_bytes(b"x")
        failures, _ = go()
        expect("other bytes under a listed name fail", any("b.osdi has sha256" in f for f in failures))
        (od / "osdi" / "b.osdi").write_bytes(b"b")
        (od / "osdi" / "c.osdi").write_bytes(b"c")
        failures, _ = go()
        expect("an unlisted OSDI file fails", any("c.osdi is in" in f for f in failures))
        os.remove(od / "osdi" / "c.osdi")
        os.remove(od / "osdi" / "a.osdi")
        failures, _ = go()
        expect("a listed OSDI file missing fails", any("a.osdi is listed and not in" in f
                                                       for f in failures))
        (od / "osdi" / "a.osdi").write_bytes(b"a")
        failures, _ = go()
        expect("restored, it passes again", failures == [])

    load_ok = "Circuit: * osdi load\n\nNo. of Data Rows : 1\n"
    open_error = 'Error opening osdi lib "%s": No such file or directory!\n' % ABSENT_OSDI
    expect("a solved load with an ablation error passes", decide_osdi(0, load_ok, open_error) == [])
    expect("a load that exits nonzero fails", len(decide_osdi(139, load_ok, open_error)) == 1)
    expect("a load that prints an error fails",
           any("printed an error" in f for f in decide_osdi(0, open_error + load_ok, open_error)))
    expect("a load that solves nothing fails",
           any("solved nothing" in f for f in decide_osdi(0, "Circuit\n", open_error)))
    expect("a silent ablation fails",
           any("ablation" in f for f in decide_osdi(0, load_ok, load_ok)))
    expect("the devhelp reading finds the OSDI devices",
           OSDI_DEVICE.findall("PSP103VA             :\tA simulator independent device "
                               "loaded with OSDI\nresistor : Simple linear resistor\n")
           == ["PSP103VA"])

    q1 = "0.000000000e+00    0s\n1.000100000e-05    1s\n"
    poison = "/tmp/w/poison/libvvp.so"
    shim = "Icarus shim failed to find VVP function: %s: undefined symbol: x.\n" % poison
    expect("both explicit runs at q=1 and the by-name poison pass",
           decide_cosim((0, q1), (0, q1), (134, shim), poison) == [])
    expect("an explicit run that never reaches 1 fails",
           len(decide_cosim((0, "0.0e+00    0s\n"), (0, q1), (134, shim), poison)) == 1)
    expect("an explicit run that exits nonzero fails",
           len(decide_cosim((0, q1), (1, q1), (134, shim), poison)) == 1)
    expect("an explicit run that says it failed fails",
           len(decide_cosim((0, q1 + "cannot open x\n"), (0, q1), (134, shim), poison)) == 1)
    expect("a by-name run that took another file fails",
           len(decide_cosim((0, q1), (0, q1), (134, shim.replace(poison, "/other/libvvp.so")),
                            poison)) == 1)
    expect("a by-name run that ran fails", len(decide_cosim((0, q1), (0, q1), (0, q1), poison)) == 1)

    for name, ok in cases:
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    failed = [name for name, ok in cases if not ok]
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    required = ("closure", "store", "osdi-sha256", "ngspice", "iverilog", "osdi")
    for name in required:
        ap.add_argument("--" + name)
    ap.add_argument("--cc", default="cc", help="the C compiler that builds the poison library")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    missing = [n for n in required if not getattr(args, n.replace("-", "_"))]
    if missing:
        ap.error("missing --%s (unless --self-test)" % ", --".join(missing))
    failures, passed = check(args, os.environ)
    for line in passed:
        print(line)
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("the image's mixed-signal engine is the pinned one, and it runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
