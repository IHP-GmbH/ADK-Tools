#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The Verilog-AMS engine the image ships is the one staging.pin names, and it builds and solves a model.

The image carries gnucap and modelgen-verilog, in one prefix, and the stdenv's
GCC as Nix store paths (staging.pin, "Verilog-AMS engine"), and VAMS_DEVSHELL
links into them. A run turns a Verilog-AMS module into C++ with gnucap-mg-vams,
builds it into a plugin with that GCC and loads it into gnucap. gnucap exits 0
when a plugin fails to load and when a deck fails, so nothing here is judged by
an exit code alone: the run has to print the solution.

Checked in this order. Every failure is reported, and a check that depends on
one that failed is not run:

  1. the closure list holds the two pins, and the store holds every path it
     lists (the co-simulation leg holds the store to all lists together);
  2. the devshell link resolves to the devshell pin, whose bin/ holds exactly
     gnucap, gnucap-conf, gnucap-mg-vams and gnucap-modelgen, links into the
     gnucap pin's bin/, and g++ and c++, links into one other store path's
     bin/;
  3. gnucap-conf, run from the devshell, names include and configuration
     directories inside the gnucap pin, and none of the startup files gnucap
     would read for the run exists: the root configuration its walk up from
     the run directory finds, gnucap.rc on its system start path (the run
     directory, its .gnucap/, and the pin's etc/ and etc/gnucap/) and
     .gnucaprc in the run's home;
  4. live, with an environment of PATH (the devshell's bin/ only), HOME (an
     empty directory) and LC_ALL alone, so neither LD_LIBRARY_PATH nor
     GNUCAP_PLUGPATH: gnucap-mg-vams writes C++ for a linear resistor module,
     the devshell's g++ builds it with gnucap-conf's flags, and gnucap, by the
     pin's absolute path, reads a deck on stdin that loads it and solves a
     divider of two instances. Both version lines and the default plugins'
     must read the given version, v(top) the source and v(mid) the ratio the
     two parameters give, for two pairs of parameters (so the model's own
     parameter is what moved the answer), and every plugin the run lists as
     loaded must be the model or a file under the pin's lib/gnucap/.

Usage:
    check_vams_engine.py --closure FILE --store DIR --gnucap PATH \\
        --devshell PATH --version TEXT [--link PATH]
    check_vams_engine.py --self-test
"""

import argparse
import os
import pathlib
import re
import subprocess
import sys
import tempfile

LIVE_TIMEOUT_S = 120
GNUCAP_TOOLS = ("gnucap", "gnucap-conf", "gnucap-mg-vams", "gnucap-modelgen")
COMPILERS = ("g++", "c++")

# A linear resistor, with the disciplines it needs written out, so the model
# reads no header.
MODEL = """\
nature current
  access = I;
  units = "A";
  abstol = 1e-12;
endnature

nature voltage
  access = V;
  units = "V";
  abstol = 1e-6;
endnature

discipline electrical
  potential voltage;
  flow current;
enddiscipline

module rr(p, n);
  parameter real r = 1e3;
  inout p, n;
  electrical p, n;
  analog I(p, n) <+ V(p, n) / r;
endmodule
"""
SUPPLY = 1.2
# (upper r, lower r): v(mid) is SUPPLY * lower / (upper + lower).
DIVIDERS = ((3000, 1000), (1000, 1000))
DECK = """\
load mgsim
load ./rr.so
options numdgt=12
verilog

ground gnd;
vsource #(.dc({supply})) v1(top, gnd);
rr #(.r({upper})) r1(top, mid);
rr #(.r({lower})) r2(mid, gnd);

print op v(top) v(mid)
op
load
"""
REL_TOL = 1e-6
VERSION_LINES = ("main version", "core-lib version", "default plugins")
SUFFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
          "K": 1e3, "Meg": 1e6, "G": 1e9, "T": 1e12}
NUMBER = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(f|p|n|u|m|K|Meg|G|T)?$")
HEADER = re.compile(r"^#\s+v\(top\)\s+v\(mid\)\s*$")
PLUGIN = re.compile(r"^\S+\.so$")


def named(names, shown=10):
    """At most ``shown`` of ``names``, and how many more there are."""
    more = len(names) - shown
    return ", ".join(names[:shown]) + (" and %d more" % more if more > 0 else "")


def read_lines(path):
    """A list file's lines: '#' lines and blank ones skipped."""
    return [line.strip() for line in pathlib.Path(path).read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def inside(path, root):
    """Whether ``path`` is ``root`` or under it, both taken as written."""
    return path == root or path.startswith(root.rstrip("/") + "/")


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


def check_devshell(link, devshell, gnucap, store):
    """The link is the devshell pin, and its bin/ is exactly the six links."""
    failures = []
    if os.path.realpath(link) != os.path.realpath(devshell):
        return ["%s resolves to %s, not %s" % (link, os.path.realpath(link), devshell)], None
    bin_dir = os.path.join(devshell, "bin")
    held = sorted(os.listdir(bin_dir)) if os.path.isdir(bin_dir) else []
    want = sorted(GNUCAP_TOOLS + COMPILERS)
    if held != want:
        return ["%s holds %s, not exactly %s" % (bin_dir, named(held) or "nothing",
                                                   ", ".join(want))], None
    for tool in GNUCAP_TOOLS:
        real = os.path.realpath(os.path.join(bin_dir, tool))
        if real != os.path.realpath(os.path.join(gnucap, "bin", tool)):
            failures.append("%s resolves to %s, not into %s" % (tool, real, gnucap))
    compilers = set()
    for tool in COMPILERS:
        target = os.readlink(os.path.join(bin_dir, tool))
        parts = pathlib.PurePath(target).parts
        root = str(pathlib.PurePath(*parts[:len(pathlib.PurePath(store).parts) + 1]))
        if (pathlib.PurePath(root).parent != pathlib.PurePath(store) or root == gnucap
                or target != os.path.join(root, "bin", tool)):
            failures.append("%s links to %s, not to bin/%s of a store path other than gnucap's"
                            % (tool, target, tool))
        elif not os.path.exists(target):
            failures.append("%s links to %s, which does not exist" % (tool, target))
        compilers.add(root)
    if len(compilers) > 1:
        failures.append("g++ and c++ link into different store paths: %s" % ", ".join(sorted(compilers)))
    return failures, "%s: the devshell pin, its six links into gnucap and %s" % (
        link, named(sorted(compilers)))


def startup_files(argv0, cwd, sysconfdir, home):
    """Every file gnucap would read at start, as its main() looks for them.

    The root configuration: for each of the run directory and its ancestors
    (one fewer than the slashes in its path), <dir>/ + "/." + argv0; the first
    found replaces the walk, and the config read is <that>/config, else the
    "config" the walk ends on. Then gnucap.rc on ".:.gnucap:<sysconfdir>:
    <sysconfdir>/gnucap" from the run directory, and .gnucaprc in the home.
    """
    candidates = []
    walk = cwd
    for _ in range(cwd.count("/") - 1):
        probe = walk.rstrip("/") + "/" + "/." + argv0
        candidates.append(probe)
        if os.path.exists(probe):
            walk = probe
            break
        walk = walk + "/.."
    candidates.append("/" + walk + "/config")
    for d in (".", ".gnucap", sysconfdir, sysconfdir + "/gnucap"):
        candidates.append(os.path.join(cwd, d, "gnucap.rc"))
    candidates.append(os.path.join(home, ".gnucaprc"))
    return candidates


def squash(path):
    """A path with '..' resolved and its leading slashes made one, for comparison."""
    return re.sub(r"^/+", "/", os.path.normpath(path))


def number(text):
    """A number as gnucap prints it, with its scale suffix."""
    m = NUMBER.match(text)
    if not m:
        raise ValueError("not a gnucap number: %r" % text)
    return float(m[1]) * SUFFIX.get(m[2] or "", 1.0)


def decide_run(rc, out, version, gnucap, upper, lower):
    """What is wrong with one deck's run, from its exit code and stdout."""
    failures = []
    if rc != 0:
        failures.append("exited %d" % rc)
    lines = out.splitlines()
    for key in VERSION_LINES:
        got = [line[len(key) + 2:] for line in lines if line.startswith(key + ": ")]
        if got != [version]:
            failures.append("%s reads %s, not %r" % (key, got or "nothing", version))
    heads = [i for i, line in enumerate(lines) if HEADER.match(line)]
    if len(heads) != 1 or heads[0] + 1 >= len(lines):
        failures.append("printed %d v(top) v(mid) header(s) with a row after it" % len(heads))
    else:
        row = lines[heads[0] + 1].split()
        try:
            if len(row) != 3:
                raise ValueError("a row of %d fields: %r" % (len(row), lines[heads[0] + 1]))
            top, mid = number(row[1]), number(row[2])
        except ValueError as e:
            failures.append("the op row does not read: %s" % e)
        else:
            want = SUPPLY * lower / (upper + lower)
            if abs(top - SUPPLY) > REL_TOL * SUPPLY:
                failures.append("v(top) is %r, not %r" % (top, SUPPLY))
            if abs(mid - want) > REL_TOL * want:
                failures.append("v(mid) is %r, not %r (r1 %d, r2 %d)" % (mid, want, upper, lower))
    plugins = [line for line in lines if PLUGIN.match(line)]
    if "./rr.so" not in plugins:
        failures.append("the model ./rr.so is not among the loaded plugins")
    pin_lib = gnucap.rstrip("/") + "/lib/gnucap/"
    if not any(p.startswith(pin_lib) for p in plugins):
        failures.append("no loaded plugin is under %s" % pin_lib)
    stray = [p for p in plugins if p != "./rr.so" and not p.startswith(pin_lib)]
    if stray:
        failures.append("loaded plugin(s) neither the model nor under %s: %s" % (pin_lib, named(stray)))
    return failures


def run(argv, env, cwd, stdin=None):
    """A command's exit code, stdout and stderr; a timeout is exit code -1."""
    try:
        done = subprocess.run(argv, env=env, cwd=cwd, input=stdin, capture_output=True,
                              text=True, timeout=LIVE_TIMEOUT_S)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as e:
        return -1, e.stdout or "", "timed out after %d s" % LIVE_TIMEOUT_S
    except OSError as e:
        return -1, "", str(e)


def check_live(devshell, gnucap, version, work):
    """Build the model and solve the dividers; failures, notes, and the output to show."""
    failures, notes, out = [], [], {}
    home = work / "home"
    home.mkdir()
    env = {"PATH": os.path.join(devshell, "bin"), "HOME": str(home), "LC_ALL": "C"}
    tool = lambda name: os.path.join(devshell, "bin", name)  # noqa: E731

    conf = {}
    for key in ("cppflags", "sysconfdir"):
        rc, text, err = run([tool("gnucap-conf"), "--" + key], env, work)
        conf[key] = text.strip()
        if rc != 0 or not conf[key]:
            failures.append("gnucap-conf --%s exited %d and printed %r: %s"
                            % (key, rc, conf[key], err.strip()))
    if failures:
        return failures, notes, out
    if conf["cppflags"] != "-I%s/include/gnucap" % gnucap:
        failures.append("gnucap-conf --cppflags is %r, not -I%s/include/gnucap"
                        % (conf["cppflags"], gnucap))
    if not inside(conf["sysconfdir"], gnucap):
        failures.append("gnucap-conf --sysconfdir is %r, not inside %s" % (conf["sysconfdir"], gnucap))
    found = [f for f in startup_files(os.path.join(gnucap, "bin", "gnucap"), str(work),
                                      conf["sysconfdir"], str(home)) if os.path.exists(f)]
    if found:
        failures.append("startup file(s) gnucap would read: %s" % named(found))
    if failures:
        return failures, notes, out
    notes.append("live: no startup file; gnucap-conf names %s/include/gnucap" % gnucap)

    (work / "rr.vams").write_text(MODEL)
    rc, text, err = run([tool("gnucap-mg-vams"), "--cc", "rr.vams"], env, work)
    out["gnucap-mg-vams"] = (rc, err)
    if rc != 0 or "rr" not in text:
        return failures + ["gnucap-mg-vams exited %d and wrote %d bytes" % (rc, len(text))], notes, out
    (work / "rr.cc").write_text(text)
    rc, _, err = run([tool("g++"), "-xc++", conf["cppflags"], "-fPIC", "-shared",
                      "rr.cc", "-o", "rr.so"], env, work)
    out["g++"] = (rc, err)
    if rc != 0 or not (work / "rr.so").is_file():
        return failures + ["g++ exited %d and wrote no rr.so" % rc], notes, out
    out.clear()
    notes.append("live: gnucap-mg-vams wrote C++ for the model and the devshell's g++ built it")

    for upper, lower in DIVIDERS:
        deck = DECK.format(supply=SUPPLY, upper=upper, lower=lower)
        rc, text, err = run([os.path.join(gnucap, "bin", "gnucap")], env, work, stdin=deck)
        found = decide_run(rc, text, version, gnucap, upper, lower)
        if found:
            name = "gnucap (r1 %d, r2 %d)" % (upper, lower)
            failures.extend("%s: %s" % (name, f) for f in found)
            out[name] = (rc, text + err)
    if not failures:
        notes.append("live: gnucap %s solved both dividers through the model, loading nothing "
                     "but it and the pin's plugins" % version)
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

    pins = [args.gnucap, args.devshell]
    if not step("closure", check_store(read_lines(args.closure), args.store, pins)):
        return failures, passed
    if not step("devshell", check_devshell(args.link, args.devshell, args.gnucap, args.store)):
        return failures, passed
    if live:
        with tempfile.TemporaryDirectory() as tmp:
            found, notes, out = check_live(args.devshell, args.gnucap, args.version,
                                           pathlib.Path(tmp))
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
        names = {"gnucap": "a" * 32 + "-gnucap-b6678fe-modelgen-verilog-100e746",
                 "devshell": "b" * 32 + "-vams-devshell", "cc": "c" * 32 + "-gcc-wrapper-14.3.0",
                 "other": "d" * 32 + "-gcc-wrapper-13.3.0"}
        paths = {k: str(store / v) for k, v in names.items()}
        for p in paths.values():
            os.makedirs(os.path.join(p, "bin"))
        for tool in GNUCAP_TOOLS:
            pathlib.Path(paths["gnucap"], "bin", tool).write_text("")
        for tool in COMPILERS:
            pathlib.Path(paths["cc"], "bin", tool).write_text("")
            pathlib.Path(paths["other"], "bin", tool).write_text("")
        shell_bin = pathlib.Path(paths["devshell"], "bin")
        for tool in GNUCAP_TOOLS:
            os.symlink(os.path.join(paths["gnucap"], "bin", tool), shell_bin / tool)
        for tool in COMPILERS:
            os.symlink(os.path.join(paths["cc"], "bin", tool), shell_bin / tool)
        closure = root / "closure.txt"
        listed = [paths["gnucap"], paths["devshell"], paths["cc"]]
        closure.write_text("# header\n" + "".join(sorted(p + "\n" for p in listed)))
        link = root / "vams-devshell"
        os.symlink(paths["devshell"], link)
        args = argparse.Namespace(closure=str(closure), store=str(store), link=str(link),
                                  gnucap=paths["gnucap"], devshell=paths["devshell"],
                                  version="trace_adv 2026.09.25")

        def go():
            return check(args, live=False)

        failures, passed = go()
        expect("the image's engine passes", failures == [] and len(passed) == 2)

        closure.write_text("".join(p + "\n" for p in sorted(listed) if "devshell" not in p))
        failures, _ = go()
        expect("a pin left off the list fails",
               any("%s is not in the closure list" % paths["devshell"] in f for f in failures))
        closure.write_text("".join(sorted(p + "\n" for p in listed + [str(store / ("e" * 32 + "-x"))])))
        failures, _ = go()
        expect("a listed path the store lacks fails",
               any("lacks 1 listed path(s): " + "e" * 32 + "-x" in f for f in failures))
        closure.write_text("".join(sorted(p + "\n" for p in listed)))

        os.remove(link)
        os.symlink(paths["gnucap"], link)
        failures, _ = go()
        expect("a link to another path fails", any("vams-devshell resolves to" in f for f in failures))
        os.remove(link)
        os.symlink(paths["devshell"], link)

        pathlib.Path(shell_bin, "ngspice").write_text("")
        failures, _ = go()
        expect("a seventh entry in bin/ fails", any("not exactly" in f for f in failures))
        os.remove(shell_bin / "ngspice")
        os.remove(shell_bin / "gnucap-mg-vams")
        failures, _ = go()
        expect("a missing tool fails", any("not exactly" in f for f in failures))
        os.symlink(os.path.join(paths["cc"], "bin", "g++"), shell_bin / "gnucap-mg-vams")
        failures, _ = go()
        expect("a tool linked elsewhere fails",
               any("gnucap-mg-vams resolves to" in f for f in failures))
        os.remove(shell_bin / "gnucap-mg-vams")
        os.symlink(os.path.join(paths["gnucap"], "bin", "gnucap-mg-vams"), shell_bin / "gnucap-mg-vams")
        os.remove(shell_bin / "c++")
        os.symlink(os.path.join(paths["other"], "bin", "c++"), shell_bin / "c++")
        failures, _ = go()
        expect("g++ and c++ from two compilers fail",
               any("different store paths" in f for f in failures))
        os.remove(shell_bin / "c++")
        os.symlink(os.path.join(paths["gnucap"], "bin", "gnucap"), shell_bin / "c++")
        failures, _ = go()
        expect("a compiler link into gnucap fails",
               any("c++ links to" in f for f in failures))
        os.remove(shell_bin / "c++")
        os.symlink(os.path.join(paths["cc"], "bin", "c++"), shell_bin / "c++")
        failures, _ = go()
        expect("restored, it passes again", failures == [])

        # The startup walk, against a tree laid out the way gnucap reads it.
        run_dir = root / "w" / "x"
        run_dir.mkdir(parents=True)
        home = root / "home"
        home.mkdir()
        etc = os.path.join(paths["gnucap"], "etc")
        argv0 = os.path.join(paths["gnucap"], "bin", "gnucap")
        files = startup_files(argv0, str(run_dir), etc, str(home))
        expect("the walk probes each ancestor but the root, then one config",
               len(files) == str(run_dir).count("/") - 1 + 1 + 4 + 1)
        expect("the first probe is the run directory joined to /.<argv0>",
               files[0] == str(run_dir) + "//." + argv0)
        expect("the walk ends on the config at the top of the run directory's path",
               squash(files[str(run_dir).count("/") - 1]) == "/" + str(run_dir).split("/")[1] + "/config")
        expect("a bare tree has no startup file", not [f for f in files if os.path.exists(f)])
        for place in (run_dir / "gnucap.rc", run_dir / ".gnucap" / "gnucap.rc",
                      pathlib.Path(etc, "gnucap.rc"), pathlib.Path(etc, "gnucap", "gnucap.rc"),
                      home / ".gnucaprc"):
            place.parent.mkdir(parents=True, exist_ok=True)
            place.write_text("")
            hit = [f for f in startup_files(argv0, str(run_dir), etc, str(home)) if os.path.exists(f)]
            expect("%s is found" % place.relative_to(root), len(hit) == 1)
            place.unlink()
        (run_dir / ".gnucap").rmdir()
        bare = startup_files("gnucap", str(run_dir), etc, str(home))
        (root / "w" / ".gnucap").mkdir()
        (root / "w" / ".gnucap" / "config").write_text("")
        walked = startup_files("gnucap", str(run_dir), etc, str(home))
        expect("a .gnucap in an ancestor ends the walk on its config",
               walked != bare and squash(walked[-6]) == str(root / "w" / ".gnucap" / "config")
               and os.path.exists(walked[-6]))

    expect("gnucap's suffixes read", [number(t) for t in ("2.p", "13.459n", "1.2", "-4.705881n",
                                                         "300.m", "1.5K", "2.Meg")]
           == [2e-12, 13.459e-9, 1.2, -4.705881e-9, 0.3, 1500.0, 2e6])
    try:
        number("1.2x")
        expect("an unknown suffix is refused", False)
    except ValueError:
        expect("an unknown suffix is refused", True)

    pin = "/nix/store/" + "a" * 32 + "-gnucap-b6678fe-modelgen-verilog-100e746"
    version = "trace_adv 2026.09.25"

    def output(top="1.199999994", mid="0.5999999967", plugins=None, versions=None):
        head = ["Gnucap : The Gnu Circuit Analysis Package."]
        head += ["%s: %s" % (k, v) for k, v in zip(VERSION_LINES, versions or [version] * 3)]
        body = ["param: already installed, replacing",
                "#                  v(top)            v(mid)           ",
                " 27.               %s       %s     " % (top, mid)]
        listed = plugins if plugins is not None else [
            "./rr.so", pin + "/lib/gnucap/analysis/gnucap-analysis.so",
            pin + "/lib/gnucap/mgsim/lang_verilog.so"]
        return "\n".join(head + body + listed) + "\n"

    expect("a run that solves the divider passes", decide_run(0, output(), version, pin, 1000, 1000) == [])
    expect("the same row against another ratio fails",
           any("v(mid)" in f for f in decide_run(0, output(), version, pin, 3000, 1000)))
    expect("a nonzero exit fails", "exited 1" in decide_run(1, output(), version, pin, 1000, 1000))
    expect("another core-lib version fails",
           any("core-lib version" in f for f in decide_run(
               0, output(versions=[version, "trace_adv 2024.01.01", version]), version, pin, 1000, 1000)))
    expect("a missing default plugins line fails",
           any("default plugins reads nothing" in f for f in decide_run(
               0, output().replace("default plugins: " + version + "\n", ""), version, pin, 1000, 1000)))
    expect("no op row fails",
           any("header" in f for f in decide_run(0, output().replace("v(mid)  ", "v(x)  "),
                                                  version, pin, 1000, 1000)))
    expect("an unreadable value fails",
           any("does not read" in f for f in decide_run(0, output(mid="nan?"), version, pin, 1000, 1000)))
    expect("a supply off by a part in a thousand fails",
           any("v(top)" in f for f in decide_run(0, output(top="1.2012"), version, pin, 1000, 1000)))
    expect("the model missing from the plugin list fails",
           any("./rr.so" in f for f in decide_run(
               0, output(plugins=[pin + "/lib/gnucap/mgsim/lang_verilog.so"]), version, pin, 1000, 1000)))
    expect("a plugin from another prefix fails",
           any("neither the model" in f for f in decide_run(
               0, output(plugins=["./rr.so", pin + "/lib/gnucap/a.so", "/usr/local/lib/gnucap/b.so"]),
               version, pin, 1000, 1000)))
    expect("no plugin under the pin fails",
           any("no loaded plugin" in f for f in decide_run(
               0, output(plugins=["./rr.so"]), version, pin, 1000, 1000)))

    for name, ok in cases:
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    failed = [name for name, ok in cases if not ok]
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    required = ("closure", "store", "gnucap", "devshell", "version")
    for name in required:
        ap.add_argument("--" + name)
    ap.add_argument("--link", default="/opt/adk-tools/vams-devshell",
                    help="the devshell link VAMS_DEVSHELL names (default /opt/adk-tools/vams-devshell)")
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
    print("the image's Verilog-AMS engine is the pinned one, and it builds and solves a model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
