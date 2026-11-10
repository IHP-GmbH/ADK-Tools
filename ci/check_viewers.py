#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The viewers the image ships are the ones staging.pin names, and each opens what it is for.

The image carries GTKWave (VCD and FST), GAW (ngspice's raw files) and the
Xschem schematic editor as Nix store paths (staging.pin, "Waveform viewers"),
and /usr/local/bin/gtkwave, /usr/local/bin/gaw and /usr/local/bin/xschem link
into them. All three are GUI programs; here they run under Xvfb, and each is
made to say what it read, so a viewer that starts and shows nothing does not
pass.

Checked in this order. Every failure is reported, and a check that depends on
one that failed is not run:

  1. the closure list holds the three pins, and the store holds every path it
     lists (the co-simulation leg holds the store to all lists together);
  2. gtkwave, gaw and xschem in the bin directory resolve to bin/gtkwave,
     bin/gaw and bin/xschem inside the pins;
  3. Xschem's share directory (XSCHEM_SHAREDIR) resolves to share/xschem in
     its pin and holds the device symbols the schematic below places;
  4. live, GTKWave: a Tcl script (-S) on a small VCD, and on the FST the pin's
     own vcd2fst made of it, prints the signal names, the time range and the
     clock's value at two times. Each has to be the file's: the names and range
     as written, 0 before the edge and 1 after it;
  5. live, GAW: it has no batch mode, so it is driven over its command socket
     (-p). Before anything is loaded it must refuse to show a variable; it then
     loads the binary raw file the image's ngspice wrote for an RC step, refuses
     a variable the file lacks, shows v(out) and v(in) and exports the waves it
     shows as text. Every exported row has to equal the raw file's own values,
     read here from its bytes, and v(out) must differ from v(in), so that the
     comparison could fail. A file GAW cannot read opens a dialog and the
     socket never answers, so no unreadable file is tried;
  6. live, Xschem: its version line must name the version in the pin's name.
     With no X it netlists a small RC schematic of the device symbols as
     SPICE, and the netlist must hold exactly the three elements drawn, with
     no symbol reported missing. Then, under Xvfb, it opens the same
     schematic with a Tcl script that writes back its name, its instance
     count, each element's symbol and value, and that the window is mapped.

Usage:
    check_viewers.py --closure FILE --store DIR --gtkwave PATH --gaw PATH \\
        --xschem PATH --ngspice PATH [--bin-dir DIR] [--xschem-sharedir DIR]
    check_viewers.py --self-test
"""

import argparse
import os
import pathlib
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

LIVE_TIMEOUT_S = 120
SOCKET_TIMEOUT_S = 30
# (in the bin directory, pin, inside the pin)
LINKS = (("gtkwave", "gtkwave", "bin/gtkwave"),
         ("gaw", "gaw", "bin/gaw"),
         ("xschem", "xschem", "bin/xschem"))
XVFB = ["xvfb-run", "-a", "--server-args=-screen 0 1280x1024x24"]

# A clock that rises at 5 ns and falls at 10.
VCD = """\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
#10
0!
"""
TCL = """\
set n [gtkwave::getNumFacs]
puts "adk-check facs $n"
for {set i 0} {$i < $n} {incr i} { puts "adk-check fac [gtkwave::getFacName $i]" }
puts "adk-check range [gtkwave::getMinTime] [gtkwave::getMaxTime]"
gtkwave::addSignalsFromList [list top.clk]
foreach t {2 7} {
    gtkwave::setMarker $t
    puts "adk-check value $t [gtkwave::getTraceValueAtMarkerFromName top.clk]"
}
gtkwave::/File/Quit
"""
GTKWAVE_WANT = {"facs": "1", "fac": ["top.clk"], "range": "0 10", "value": {"2": "0", "7": "1"}}

# An RC step: v(in) is a pulse, v(out) its low-passed copy.
DECK = """\
* rc step
V1 in 0 PULSE(0 1 1n 1n 1n 10n 20n)
R1 in out 1k
C1 out 0 1p
.tran 0.1n 40n
.end
"""
SHOWN = ("v(out)", "v(in)")
ABSENT_VAR = "v(adk_check_absent)"
NO_TABLE = "Current table not defined"
REL_TOL = 1e-9

# The same RC, drawn: each pin of a device symbol meets a net label.
SYMBOLS = ("vsource", "res", "capa", "gnd", "lab_pin")
SCH = """\
v {xschem version=3.4.8RC file_version=1.2}
G {}
K {}
V {}
S {}
E {}
C {vsource.sym} 0 0 0 0 {name=V1 value="PULSE(0 1 1n 1n 1n 10n 20n)"}
C {lab_pin.sym} 0 -30 0 0 {name=l1 lab=in}
C {gnd.sym} 0 30 0 0 {name=l2 lab=GND}
C {res.sym} 100 0 0 0 {name=R1 value=1k}
C {lab_pin.sym} 100 -30 0 0 {name=l3 lab=in}
C {lab_pin.sym} 100 30 0 0 {name=l4 lab=out}
C {capa.sym} 200 0 0 0 {name=C1 value=1p}
C {lab_pin.sym} 200 -30 0 0 {name=l5 lab=out}
C {gnd.sym} 200 30 0 0 {name=l6 lab=GND}
"""
NETLIST_WANT = ["C1 out GND 1p m=1", "R1 in out 1k m=1", "V1 in GND PULSE(0 1 1n 1n 1n 10n 20n)"]
# Xschem's Tk console swallows stdout, so the script writes to a file.
XSCHEM_TCL = """\
set f [open adk-check.out w]
puts $f "schname [file tail [xschem get schname]]"
puts $f "instances [xschem get instances]"
foreach i {V1 R1 C1} {
    puts $f "inst $i [xschem getprop instance $i cell::name] [xschem getprop instance $i value]"
}
puts $f "window [winfo exists .drw] [winfo ismapped .]"
close $f
exit
"""
XSCHEM_GUI_WANT = ["schname rc.sch", "instances 9",
                   "inst V1 vsource.sym PULSE(0 1 1n 1n 1n 10n 20n)",
                   "inst R1 res.sym 1k", "inst C1 capa.sym 1p", "window 1 1"]
XSCHEM_PIN = re.compile(r"-xschem-([.0-9]+(?:RC)?)-[0-9a-f]{7}$")


def named(names, shown=10):
    """At most ``shown`` of ``names``, and how many more there are."""
    more = len(names) - shown
    return ", ".join(names[:shown]) + (" and %d more" % more if more > 0 else "")


def read_lines(path):
    """A list file's lines: '#' lines and blank ones skipped."""
    return [line.strip() for line in pathlib.Path(path).read_text().splitlines()
            if line.strip() and not line.startswith("#")]


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


def check_links(bin_dir, pins):
    failures = []
    for name, pin, inner in LINKS:
        link = os.path.join(bin_dir, name)
        want = os.path.join(pins[pin], inner)
        real = os.path.realpath(link)
        if not os.path.exists(link):
            failures.append("%s does not resolve to anything" % link)
        elif real != os.path.realpath(want):
            failures.append("%s resolves to %s, not %s" % (link, real, want))
    return failures, "%s: gtkwave, gaw and xschem link into the pins" % bin_dir


def check_sharedir(sharedir, xschem_pin):
    want = os.path.join(xschem_pin, "share", "xschem")
    if not sharedir:
        return ["XSCHEM_SHAREDIR is not set"], None
    if os.path.realpath(sharedir) != os.path.realpath(want):
        return ["%s resolves to %s, not %s" % (sharedir, os.path.realpath(sharedir), want)], None
    devices = os.path.join(sharedir, "xschem_library", "devices")
    missing = [n + ".sym" for n in SYMBOLS if not os.path.isfile(os.path.join(devices, n + ".sym"))]
    if missing:
        return ["%s lacks %s" % (devices, ", ".join(missing))], None
    return [], "%s is the pin's, with the %d device symbols the check places" % (
        sharedir, len(SYMBOLS))


def read_raw(data):
    """An ngspice binary raw file's variable names and its points, as rows of floats."""
    head, sep, body = data.partition(b"Binary:\n")
    if not sep:
        raise ValueError("no 'Binary:' line; not a binary raw file")
    text = head.decode("ascii", "replace")
    fields = dict(re.findall(r"^([A-Za-z. ]+):[ \t]*(.*)$", text, re.M))
    if "complex" in fields.get("Flags", ""):
        raise ValueError("complex data; only real is read here")
    nvars, npoints = int(fields["No. Variables"]), int(fields["No. Points"])
    block = re.split(r"^Variables:[ \t]*$", text, maxsplit=1, flags=re.M)[-1]
    names = [line.split()[1] for line in block.strip().splitlines()[:nvars]]
    if len(names) != nvars or len(body) != 8 * nvars * npoints:
        raise ValueError("%d variables and %d points do not fit %d bytes of data"
                         % (nvars, npoints, len(body)))
    flat = struct.unpack("<%dd" % (nvars * npoints), body)
    return names, [list(flat[i * nvars:(i + 1) * nvars]) for i in range(npoints)]


def read_export(text):
    """GAW's text export: a header of names, then one row of numbers per point."""
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("the export is empty")
    return lines[0], [[float(x) for x in row] for row in lines[1:]]


def close(a, b, scale):
    return abs(a - b) <= REL_TOL * scale


def column(rows, i):
    return [row[i] for row in rows]


def decide_gaw(replies, raw, export):
    """Failures from GAW's socket replies, the raw file and GAW's export."""
    failures = []
    if NO_TABLE not in replies.get("before", ""):
        failures.append("before any load, showing a variable was not refused with "
                        "'%s' (replied %r), so a refusal later shows nothing"
                        % (NO_TABLE, replies.get("before")))
    for step in ("load",) + tuple("show " + v for v in SHOWN) + ("export",):
        if replies.get(step) != "":
            failures.append("%s: GAW replied %r, not an empty line" % (step, replies.get(step)))
    if "Variable %s not defined" % ABSENT_VAR not in replies.get("absent", ""):
        failures.append("a variable the file lacks was not refused (replied %r)"
                        % replies.get("absent"))
    if failures:
        return failures
    names, points = raw
    header, rows = export
    want = [names[0]] + list(SHOWN)
    if header != want:
        return ["the export's columns are %s, not %s" % (header, want)]
    if len(rows) != len(points):
        return ["the export has %d rows, the raw file %d points" % (len(rows), len(points))]
    for col, name in enumerate(want):
        ref = column(points, names.index(name))
        scale = max(abs(v) for v in ref) or 1.0
        bad = [i for i, (a, b) in enumerate(zip(column(rows, col), ref)) if not close(a, b, scale)]
        if bad:
            failures.append("%s differs from the raw file at %d of %d rows, first at row %d: "
                            "%r against %r" % (name, len(bad), len(rows), bad[0],
                                               rows[bad[0]][col], ref[bad[0]]))
    out, inp = (column(points, names.index(v)) for v in SHOWN)
    scale = max(abs(v) for v in inp) or 1.0
    if all(close(a, b, scale) for a, b in zip(out, inp)):
        failures.append("v(out) equals v(in) in the raw file, so the comparison could not fail")
    return failures


def decide_gtkwave(rc, out):
    """Failures from one GTKWave run of the Tcl script."""
    if rc != 0:
        return ["exited %d" % rc]
    got = {"fac": [], "value": {}}
    for line in out.splitlines():
        if not line.startswith("adk-check "):
            continue
        key, _, rest = line[len("adk-check "):].partition(" ")
        if key == "fac":
            got["fac"].append(rest)
        elif key == "value":
            t, _, v = rest.partition(" ")
            got["value"][t] = v
        else:
            got[key] = rest
    failures = []
    for key, want in GTKWAVE_WANT.items():
        if got.get(key) != want:
            failures.append("%s read %r, the file has %r" % (key, got.get(key), want))
    return failures


def xschem_version(pin):
    """The version in an Xschem pin's name, or None."""
    m = XSCHEM_PIN.search(pin)
    return m.group(1) if m else None


def decide_xschem_version(rc, out, pin):
    """Failures from xschem -v. The version line need not come first: with no
    ~/.xschem yet, Xschem makes one and says so, then names its share
    directory and the init files it sources."""
    want = "XSCHEM V%s" % xschem_version(pin)
    if rc != 0:
        return ["xschem -v exited %d" % rc]
    lines = [line.strip() for line in out.splitlines()]
    line = next((line for line in lines if line.startswith("XSCHEM V")), None)
    if line is None:
        return ["no line starts with 'XSCHEM V', the pin's name says %r" % want]
    if line != want:
        return ["the version line is %r, the pin's name says %r" % (line, want)]
    return []


def decide_xschem_netlist(rc, text):
    """Failures from the batch netlist: its exit and the elements it wrote."""
    if rc != 0:
        return ["netlisting exited %d" % rc]
    failures = []
    missing = [line for line in text.splitlines() if "IS MISSING" in line]
    if missing:
        failures.append("%d symbol(s) reported missing: %s" % (len(missing), named(missing, 3)))
    got = sorted(line.strip() for line in text.splitlines()
                 if line.strip() and line.strip()[0] not in "*.")
    if got != NETLIST_WANT:
        failures.append("the netlist's elements are %r, the schematic's %r" % (got, NETLIST_WANT))
    return failures


def decide_xschem_gui(rc, text):
    """Failures from the GUI run of the Tcl script, from what it wrote back."""
    if rc != 0:
        return ["exited %d" % rc]
    got = [line.strip() for line in text.splitlines() if line.strip()]
    if got != XSCHEM_GUI_WANT:
        return ["the script wrote %r, the schematic has %r" % (got, XSCHEM_GUI_WANT)]
    return []


def run(argv, cwd, env, timeout=LIVE_TIMEOUT_S):
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors="replace",
                           timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "%s: %s" % (argv[0], e)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def drive_gaw(gaw, raw, export, work, env):
    """GAW under Xvfb, driven over its socket; its replies and what it printed."""
    port = free_port()
    log = open(work / "gaw.out", "w")
    p = subprocess.Popen(XVFB + [gaw, "-p", str(port)], cwd=work, env=env,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    replies = {}
    try:
        deadline = time.monotonic() + SOCKET_TIMEOUT_S
        while True:
            try:
                conn = socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT_S)
                break
            except OSError:
                if p.poll() is not None or time.monotonic() > deadline:
                    replies["start"] = "no socket on port %d (GAW %s)" % (
                        port, "exited %d" % p.returncode if p.poll() is not None else "still running")
                    return replies
                time.sleep(0.2)
        with conn, conn.makefile("rwb", buffering=0) as f:
            for step, line in (("before", "copyvar %s p0" % SHOWN[0]),
                               ("load", "load %s spice3raw" % raw),
                               ("absent", "copyvar %s p0" % ABSENT_VAR),
                               ("show " + SHOWN[0], "copyvar %s p0" % SHOWN[0]),
                               ("show " + SHOWN[1], "copyvar %s p1" % SHOWN[1]),
                               ("export", "export_data %s ascii" % export)):
                f.write((line + "\n").encode())
                answer = f.readline()
                if not answer.endswith(b"\n"):
                    replies[step] = "no answer"
                    return replies
                replies[step] = answer.decode("utf-8", "replace").rstrip("\n")
    except OSError as e:
        replies["socket"] = str(e)
    finally:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(10)
        log.close()
    return replies


def check_xschem(bin_dir, xschem_pin, work, env):
    """Failures, notes and output of Xschem's three runs."""
    failures, notes, out = [], [], {}
    xschem = os.path.join(bin_dir, "xschem")
    out["xschem -v"] = run([xschem, "-v"], work, env)
    failures.extend(decide_xschem_version(*out["xschem -v"], xschem_pin))
    (work / "rc.sch").write_text(SCH)
    out["xschem netlist"] = run([xschem, "-x", "-q", "-n", "-s", "-o", str(work),
                                 "-N", "rc.spice", "rc.sch"], work, env)
    try:
        text = (work / "rc.spice").read_text(errors="replace")
    except OSError as e:
        failures.append("no netlist written: %s" % e)
    else:
        out["xschem netlist"] = (out["xschem netlist"][0],
                                 out["xschem netlist"][1] + "\nnetlist:\n" + text)
        failures.extend(decide_xschem_netlist(out["xschem netlist"][0], text))
    (work / "probe.tcl").write_text(XSCHEM_TCL)
    out["xschem gui"] = run(XVFB + [xschem, "--script", "probe.tcl", "rc.sch"], work, env)
    try:
        text = (work / "adk-check.out").read_text(errors="replace")
    except OSError as e:
        failures.append("the GUI run wrote nothing back: %s" % e)
    else:
        failures.extend(decide_xschem_gui(out["xschem gui"][0], text))
    if not failures:
        notes.append("live: xschem printed XSCHEM V%s, netlisted the RC schematic to its three "
                     "elements and, under Xvfb, opened it with its 9 instances"
                     % xschem_version(xschem_pin))
    return ["xschem: %s" % f for f in failures], notes, out


def check_live(bin_dir, gtkwave_pin, ngspice, work, xschem_pin=None):
    """Failures, notes, and the runs' output to show when anything failed."""
    failures, notes, out = [], [], {}
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_LIBRARY_PATH", "SPICE_ASCIIRAWFILE", "DISPLAY")}
    env["HOME"] = str(work / "home")
    (work / "home").mkdir()
    if xschem_pin:
        found, more, out = check_xschem(bin_dir, xschem_pin, work, env)
        failures.extend(found)
        notes.extend(more)

    (work / "clk.vcd").write_text(VCD)
    (work / "names.tcl").write_text(TCL)
    out["vcd2fst"] = run([os.path.join(gtkwave_pin, "bin", "vcd2fst"), "clk.vcd", "clk.fst"],
                         work, env)
    gtkwave = os.path.join(bin_dir, "gtkwave")
    for kind in ("vcd", "fst"):
        out["gtkwave " + kind] = run(XVFB + [gtkwave, "-S", "names.tcl", "clk." + kind], work, env)
        found = decide_gtkwave(*out["gtkwave " + kind])
        failures.extend("gtkwave on the %s: %s" % (kind.upper(), f) for f in found)
    if not any(f.startswith("gtkwave") for f in failures):
        notes.append("live: gtkwave read top.clk, 0..10 ns, 0 at 2 and 1 at 7, from the VCD "
                     "and from the FST vcd2fst made")

    (work / "rc.cir").write_text(DECK)
    out["ngspice"] = run([ngspice, "-b", "-r", "rc.raw", "rc.cir"], work, env)
    try:
        raw = read_raw((work / "rc.raw").read_bytes())
    except (OSError, ValueError, KeyError, IndexError) as e:
        failures.append("ngspice wrote no binary raw file this can read (exit %d): %s"
                        % (out["ngspice"][0], e))
        return failures, notes, out
    replies = drive_gaw(os.path.join(bin_dir, "gaw"), work / "rc.raw", work / "rc.dat", work, env)
    out["gaw"] = (0, (work / "gaw.out").read_text(errors="replace") + "\nreplies: %r\n" % replies)
    for step in ("start", "socket"):
        if step in replies:
            return failures + ["gaw: %s" % replies[step]], notes, out
    try:
        export = read_export((work / "rc.dat").read_text())
    except (OSError, ValueError) as e:
        export = ([], [])
        failures.append("gaw: no export to read: %s" % e)
    found = decide_gaw(replies, raw, export)
    failures.extend("gaw: %s" % f for f in found)
    if not found and export[1]:
        notes.append("live: gaw loaded ngspice's binary raw file (%d points), refused %s and, "
                     "before the load, any variable, and exported v(out) and v(in) equal to "
                     "the file's" % (len(raw[1]), ABSENT_VAR))
    return failures, notes, out


def check(args, live=True):
    """Every failure, and what passed, in order."""
    failures, passed = [], []

    def step(name, result):
        found, note = result
        failures.extend("%s: %s" % (name, f) for f in found)
        if not found and note:
            passed.append("%s: %s" % (name, note))
        return not found

    pins = {"gtkwave": args.gtkwave, "gaw": args.gaw, "xschem": args.xschem}
    if not step("closure", check_store(read_lines(args.closure), args.store, list(pins.values()))):
        return failures, passed
    if not step("links", check_links(args.bin_dir, pins)):
        return failures, passed
    if not step("sharedir", check_sharedir(args.xschem_sharedir, args.xschem)):
        return failures, passed
    if live:
        with tempfile.TemporaryDirectory() as tmp:
            found, notes, out = check_live(args.bin_dir, args.gtkwave, args.ngspice,
                                           pathlib.Path(tmp), args.xschem)
        failures.extend("live: %s" % f for f in found)
        passed.extend(notes)
        if found:
            for name, (rc, text) in out.items():
                failures.append("live: the %s run (exit %d) printed:\n%s" % (name, rc, text.rstrip()))
    return failures, passed


def raw_bytes(names, points, flags="real"):
    head = ["Title: * t", "Plotname: Transient Analysis", "Flags: %s" % flags,
            "No. Variables: %d" % len(names), "No. Points: %d" % len(points), "Variables:"]
    head += ["\t%d\t%s\tvoltage" % (i, n) for i, n in enumerate(names)]
    flat = [v for row in points for v in row]
    return ("\n".join(head) + "\nBinary:\n").encode() + struct.pack("<%dd" % len(flat), *flat)


def self_test():
    cases = []

    def expect(name, ok):
        cases.append((name, ok))

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        store = root / "store"
        names = {"gtkwave": "a" * 32 + "-gtkwave-3.3.121", "gaw": "b" * 32 + "-gaw3-20250128",
                 "xschem": "d" * 32 + "-xschem-3.4.8RC-8d2088e",
                 "gtk": "c" * 32 + "-gtk+3-3.24.49"}
        paths = {k: str(store / v) for k, v in names.items()}
        for p in paths.values():
            os.makedirs(os.path.join(p, "bin"))
        devices = pathlib.Path(paths["xschem"], "share", "xschem", "xschem_library", "devices")
        devices.mkdir(parents=True)
        for n in SYMBOLS:
            (devices / (n + ".sym")).write_text("")
        sharedir = root / "xschem-share"
        os.symlink(os.path.join(paths["xschem"], "share", "xschem"), sharedir)
        for name, pin, inner in LINKS:
            pathlib.Path(paths[pin], inner).write_text("")
        closure = root / "closure.txt"
        closure.write_text("# header\n" + "".join(sorted(p + "\n" for p in paths.values())))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, pin, inner in LINKS:
            os.symlink(os.path.join(paths[pin], inner), bin_dir / name)
        args = argparse.Namespace(closure=str(closure), store=str(store), bin_dir=str(bin_dir),
                                  gtkwave=paths["gtkwave"], gaw=paths["gaw"],
                                  xschem=paths["xschem"], xschem_sharedir=str(sharedir),
                                  ngspice="ngspice")

        def go(a=args):
            return check(a, live=False)

        failures, passed = go()
        expect("the image's viewers pass", failures == [] and len(passed) == 3)

        closure.write_text("".join(p + "\n" for p in sorted(paths.values()) if "gaw" not in p))
        failures, _ = go()
        expect("a pin left off the list fails",
               any("%s is not in the closure list" % paths["gaw"] in f for f in failures))
        closure.write_text("".join(sorted(p + "\n" for p in paths.values())))
        os.rmdir(os.path.join(paths["gtk"], "bin"))
        os.rmdir(paths["gtk"])
        failures, _ = go()
        expect("a listed path the store lacks fails",
               any("lacks 1 listed path(s): " + names["gtk"] in f for f in failures))
        os.makedirs(paths["gtk"])

        os.remove(bin_dir / "gaw")
        failures, _ = go()
        expect("a missing link fails", any("gaw does not resolve" in f for f in failures))
        os.symlink(os.path.join(paths["gtkwave"], "bin", "gtkwave"), bin_dir / "gaw")
        failures, _ = go()
        expect("a link into the other viewer fails", any("gaw resolves to" in f for f in failures))
        os.remove(bin_dir / "gaw")
        os.symlink(os.path.join(paths["gaw"], "bin", "gaw"), bin_dir / "gaw")
        failures, _ = go()
        expect("restored, it passes again", failures == [])

        os.remove(devices / "res.sym")
        failures, _ = go()
        expect("a device symbol missing from the share directory fails",
               any("lacks res.sym" in f for f in failures))
        (devices / "res.sym").write_text("")
        failures, _ = go(argparse.Namespace(**dict(vars(args), xschem_sharedir=paths["gaw"])))
        expect("a share directory outside the pin fails",
               any("resolves to %s" % paths["gaw"] in f for f in failures))
        failures, _ = go(argparse.Namespace(**dict(vars(args), xschem_sharedir=None)))
        expect("no share directory fails", any("XSCHEM_SHAREDIR is not set" in f for f in failures))

    good = "\n".join(["GTKWave Analyzer v3.3.121", "adk-check facs 1", "adk-check fac top.clk",
                      "adk-check range 0 10", "adk-check value 2 0", "adk-check value 7 1",
                      "Exiting."]) + "\n"
    expect("GTKWave reading the file passes", decide_gtkwave(0, good) == [])
    expect("GTKWave exiting nonzero fails", decide_gtkwave(1, good) == ["exited 1"])
    expect("a clock read the same at both times fails",
           any("value" in f for f in decide_gtkwave(0, good.replace("value 2 0", "value 2 1"))))
    expect("another time range fails",
           any("range" in f for f in decide_gtkwave(0, good.replace("range 0 10", "range 0 0"))))
    expect("a run that printed nothing of the file fails",
           len(decide_gtkwave(0, "GTKWave Analyzer v3.3.121\nExiting.\n")) == 4)

    pin = "/nix/store/%s-xschem-3.4.8RC-8d2088e" % ("d" * 32)
    version = "XSCHEM V3.4.8RC\nCopyright (C) 1998-2024 Stefan Schippers\n"
    expect("the version line the pin's name gives passes",
           decide_xschem_version(0, version, pin) == [])
    expect("another version line fails",
           decide_xschem_version(0, version.replace("8RC", "7"), pin) != [])
    # What the image's xschem -v printed with a fresh HOME.
    fresh = ("Created /tmp/tmp4w5hmodz/home/.xschem dir with template xschemrc\n"
             "Using run time directory XSCHEM_SHAREDIR = /opt/adk-tools/xschem/share/xschem\n"
             "Sourcing /opt/adk-tools/xschem/share/xschem/xschemrc init file\n"
             "Sourcing /tmp/tmp4w5hmodz/home/.xschem/xschemrc init file\n") + version
    expect("the version line after the lines a fresh HOME adds passes",
           decide_xschem_version(0, fresh, pin) == [])
    expect("another version line after them fails, naming it",
           decide_xschem_version(0, fresh.replace("8RC", "7"), pin)
           == ["the version line is 'XSCHEM V3.4.7', the pin's name says 'XSCHEM V3.4.8RC'"])
    expect("a run with no version line fails",
           decide_xschem_version(0, fresh.replace(version, ""), pin)
           == ["no line starts with 'XSCHEM V', the pin's name says 'XSCHEM V3.4.8RC'"])
    expect("xschem -v exiting nonzero fails",
           decide_xschem_version(1, version, pin) == ["xschem -v exited 1"])
    expect("a release version reads without RC",
           xschem_version("/nix/store/%s-xschem-3.4.7-0123abc" % ("d" * 32)) == "3.4.7")
    netlist = "\n".join(["** sch_path: /w/rc.sch", "**.subckt rc",
                         "V1 in GND PULSE(0 1 1n 1n 1n 10n 20n)", "R1 in out 1k m=1",
                         "C1 out GND 1p m=1", "**.ends", ".GLOBAL GND", ".end"]) + "\n"
    expect("the schematic's netlist passes", decide_xschem_netlist(0, netlist) == [])
    expect("a netlist with a symbol missing fails",
           any("missing" in f for f in decide_xschem_netlist(
               0, netlist.replace("R1 in out 1k m=1", "*  R1 -  res  IS MISSING !!!!"))))
    expect("a resistor between other nets fails",
           decide_xschem_netlist(0, netlist.replace("R1 in out", "R1 in GND")) != [])
    expect("netlisting exiting nonzero fails", decide_xschem_netlist(1, netlist) != [])
    gui = "\n".join(XSCHEM_GUI_WANT) + "\n"
    expect("the GUI run writing back the schematic passes", decide_xschem_gui(0, gui) == [])
    expect("a window never mapped fails",
           decide_xschem_gui(0, gui.replace("window 1 1", "window 1 0")) != [])
    expect("a schematic of other instances fails",
           decide_xschem_gui(0, gui.replace("instances 9", "instances 0")) != [])

    names = ["time", "v(in)", "v(out)", "i(v1)"]
    points = [[t * 1e-9, float(t > 1), (1 - 0.5 ** t) * 0.9, -1e-3 * (t > 1)] for t in range(8)]
    data = raw_bytes(names, points)
    raw = read_raw(data)
    expect("the raw reader gives the names and every point", raw == (names, points))
    try:
        read_raw(data[:-8])
        expect("a raw file cut short is refused", False)
    except ValueError:
        expect("a raw file cut short is refused", True)
    try:
        read_raw(raw_bytes(names, points, flags="complex"))
        expect("complex data is refused", False)
    except ValueError:
        expect("complex data is refused", True)

    def export_of(rows):
        return "time v(out) v(in)\n" + "".join(
            "%.13g %.13g %.13g\n" % (r[0], r[2], r[1]) for r in rows)

    replies = {"before": NO_TABLE, "load": "", "absent": "Variable %s not defined" % ABSENT_VAR,
               "show v(out)": "", "show v(in)": "", "export": ""}
    exported = read_export(export_of(points))
    expect("GAW's export equal to the file passes", decide_gaw(replies, raw, exported) == [])
    moved = [list(r) for r in points]
    moved[5][2] *= 1.001
    expect("one value off fails",
           any("v(out) differs" in f for f in decide_gaw(replies, raw, read_export(export_of(moved)))))
    expect("a row missing fails",
           any("rows" in f for f in decide_gaw(replies, raw, read_export(export_of(points[:-1])))))
    swapped = read_export("time v(in) v(out)\n" + export_of(points).split("\n", 1)[1])
    expect("columns in another order fail", any("columns" in f for f in
                                                decide_gaw(replies, raw, swapped)))
    flat = [[r[0], 0.5, 0.5, r[3]] for r in points]
    flat_raw = (names, flat)
    expect("a file whose two waves are the same fails",
           any("could not fail" in f for f in decide_gaw(replies, flat_raw, read_export(export_of(flat)))))
    for step, bad in (("before", ""), ("absent", ""), ("load", "Can't load file x format y"),
                      ("export", "Can't export to file x format ascii")):
        expect("a %s reply of %r fails" % (step, bad),
               decide_gaw(dict(replies, **{step: bad}), raw, exported) != [])

    for name, ok in cases:
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    failed = [name for name, ok in cases if not ok]
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    required = ("closure", "store", "gtkwave", "gaw", "xschem", "ngspice")
    for name in required:
        ap.add_argument("--" + name)
    ap.add_argument("--bin-dir", default="/usr/local/bin",
                    help="where gtkwave, gaw and xschem are linked (default /usr/local/bin)")
    ap.add_argument("--xschem-sharedir", default=os.environ.get("XSCHEM_SHAREDIR"),
                    help="Xschem's share directory (default $XSCHEM_SHAREDIR)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    missing = [n for n in required if not getattr(args, n)]
    if missing:
        ap.error("missing --%s (unless --self-test)" % ", --".join(missing))
    failures, passed = check(args)
    for line in passed:
        print(line)
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("the image's viewers are the pinned ones, and each opens what it is for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
