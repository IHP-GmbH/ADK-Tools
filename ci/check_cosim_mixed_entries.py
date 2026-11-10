#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Every file a mixed capability entry pins is in the image with those bytes.

chiplet-cosim admits a mixed run only when one entry of its capability table of
kind ``mixed`` holds the run's whole context. Part of that context is the
image's: the engine's eight files, the VPI modules a compiled file loads, the
interpreter, libpython and cocotb, and every model file each PDK's walk
reaches, with the OSDI files it names. This check reads all of it in the image,
by chiplet-cosim's own resolution, against every mixed entry of the staged
table, so a moved byte is seen before a run is refused for it.

The engine is named as chiplet-cosim's mixed tier names it in this image
(COSIM_NGSPICE_PREFIX, COSIM_IVERILOG, COSIM_LIBVVP, COSIM_STDBUF and
COSIM_COCOTB_PYTHON); the models' roots are mixed_case.roots_from's, from
PDK_ROOT and COSIM_GF180_ROOT.

Checked for each mixed entry. Every failure is reported:

  tools    the engine's files, by the name and order the context gives them;
  vpi      a small design compiled by that iverilog, with cocotb's VPI library
           put in as a run puts it, loads the modules the entry names, in its
           order and by sha256;
  cocotb   the interpreter's and libpython's sha256, cocotb's version and the
           sha256 over its installed files, as a run's probe reports them;
  spinit   the init template for the entry's OSDI count;
  models   below each PDK's root, every reached file by sha256, and every
           OSDI sha256 the entry lists is a file in libs.tech/ngspice/osdi.

Not checked: hdl_precision, runner_sha256, cosim_delay, diagnostics_sha256,
the model sections and gate_library, which the staged chiplet-cosim's code and
a run's own inputs give, not the image's files.

Usage:
    check_cosim_mixed_entries.py [--min-entries N]
    check_cosim_mixed_entries.py --self-test
Exit 0 when every entry holds, 1 when one does not, 2 on usage.
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys
import tempfile

TICK_V = """\
`timescale 1ns/1ps
module tick(output reg q);
  initial begin
    q = 1'b0;
    #10 q = 1'b1;
  end
endmodule
"""
OSDI_DIR = ("libs.tech", "ngspice", "osdi")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short(sha):
    return (sha or "-")[:8]


def check_list(what, want, got):
    """Failures where two name/sha256 lists differ, by name, then by order."""
    want_map = {row["name"]: row["sha256"] for row in want}
    got_map = {row["name"]: row["sha256"] for row in got}
    failures = []
    for name in sorted(set(want_map) | set(got_map)):
        if name not in got_map:
            failures.append("%s: %s is named by the entry, not found in the image" % (what, name))
        elif name not in want_map:
            failures.append("%s: %s is in the image, not named by the entry" % (what, name))
        elif want_map[name] != got_map[name]:
            failures.append("%s: %s is %s, the entry has %s"
                            % (what, name, short(got_map[name]), short(want_map[name])))
    if not failures and [r["name"] for r in want] != [r["name"] for r in got]:
        failures.append("%s: order %s, the entry has %s"
                        % (what, [r["name"] for r in got], [r["name"] for r in want]))
    return failures


def check_models(models, roots):
    """Failures for one entry's models blocks below the image's roots."""
    failures = []
    for block in models:
        pdk = block["pdk_name"]
        if pdk not in roots:
            failures.append("models: no root is named for %s" % pdk)
            continue
        root = pathlib.Path(roots[pdk])
        for row in block["reached"]:
            path = root / row["file"]
            if not path.is_file():
                failures.append("models: %s %s is not there" % (pdk, row["file"]))
            elif sha256_file(path) != row["sha256"]:
                failures.append("models: %s %s is %s, the entry has %s"
                                % (pdk, row["file"], short(sha256_file(path)), short(row["sha256"])))
        osdi = root.joinpath(*OSDI_DIR)
        have = {sha256_file(p) for p in osdi.glob("*.osdi") if p.is_file()} if osdi.is_dir() else set()
        failures += ["models: %s OSDI %s is no file in %s" % (pdk, short(sha), osdi)
                     for sha in block.get("osdi", []) if sha not in have]
    return failures


def check_entry(entry, image, roots):
    """Every failure of one mixed entry against what the image gives."""
    context, failures = entry["context"], []
    failures += check_list("tools", context["tools"], image["tools"])
    failures += check_list("vpi", context["vpi_modules"], image["vpi_modules"])
    for key in ("python_sha256", "libpython_sha256", "cocotb_version", "cocotb_package_sha256"):
        if image[key] != context[key]:
            failures.append("cocotb: %s is %s, the entry has %s" % (key, image[key], context[key]))
    osdi_count = sum(len(block.get("osdi", [])) for block in context["models"])
    if image["spinit"](osdi_count) != context["spinit_sha256"]:
        failures.append("spinit: the template for %d OSDI files is %s, the entry has %s"
                        % (osdi_count, short(image["spinit"](osdi_count)), short(context["spinit_sha256"])))
    failures += check_models(context["models"], roots)
    return ["%s: %s" % (entry["id"], f) for f in failures]


class ChipletCosim:
    """chiplet-cosim's own resolution, from the installed package."""

    def __init__(self):
        from chiplet_cosim import cosim_capability, mixed_case, mixed_run
        self.capability, self.case, self.run = cosim_capability, mixed_case, mixed_run

    def entries(self):
        table, wrong = self.capability.load_table()
        if wrong is not None:
            raise SystemExit("the capability table cannot be read: %s" % (wrong.detail,))
        return [e for e in table.entries if e["kind"] == "mixed"]

    def roots(self, environ):
        return {name: str(path) for name, path in self.case.roots_from(environ).items()}

    def image(self, environ, work):
        """The image's half of a mixed context, gathered as a run gathers it."""
        run = self.run
        tools = run.Tools.from_environ(environ)
        if tools.python is None:
            raise SystemExit("COSIM_COCOTB_PYTHON is not set")
        source = work / "tick.v"
        source.write_text(TICK_V, encoding="utf-8")
        vvp = work / "tick.vvp"
        code, out = run._quick([tools.iverilog, "-Wtimescale", "-o", vvp, "-s", "tick", source],
                               run._base_env(tools, work), work, run.PROBE_TIMEOUT_S)
        if code != 0:
            raise SystemExit("iverilog exited %s: %s" % (code, out.strip()[-300:]))
        probe_env = {"PATH": "%s:/usr/bin:/bin" % pathlib.Path(tools.python).parent,
                     "PYTHONNOUSERSITE": "1", "LC_ALL": "C"}
        code, out = run._quick([tools.python, "-c", run._COCOTB_PROBE], probe_env, work,
                               run.PROBE_TIMEOUT_S)
        facts = out.strip().splitlines() if code == 0 else []
        if len(facts) != 4:
            raise SystemExit("the cocotb probe did not report four facts: %s" % out.strip()[-300:])
        libs_dir, libpython, version, package = facts
        compiled = run.with_cocotb(vvp.read_text(encoding="utf-8"), pathlib.Path(libs_dir))
        modules = [run._module_file(path) for path in run.vpi_modules(compiled)]
        return {"tools": [{"name": name, "sha256": run.sha256_file(path)}
                          for name, path in run.tool_rows(tools)],
                "vpi_modules": [{"name": m.name, "sha256": run.sha256_file(m)} for m in modules],
                "python_sha256": run.sha256_file(tools.python),
                "libpython_sha256": run.sha256_file(pathlib.Path(libpython)),
                "cocotb_version": version, "cocotb_package_sha256": package,
                "spinit": lambda n: run._sha_text(run.spinit_template(n))}


def check(cosim, environ, work, min_entries):
    entries = cosim.entries()
    if len(entries) < min_entries:
        return ["entries: the capability table has %d mixed entries, fewer than %d"
                % (len(entries), min_entries)], []
    image, roots = cosim.image(environ, work), cosim.roots(environ)
    failures, passed = [], []
    for entry in entries:
        found = check_entry(entry, image, roots)
        failures += found
        if not found:
            files = sum(len(b["reached"]) + len(b.get("osdi", [])) for b in entry["context"]["models"])
            passed.append("%s: %d tools, %d VPI modules, cocotb %s, %d model and OSDI files"
                          % (entry["id"], len(image["tools"]), len(image["vpi_modules"]),
                             image["cocotb_version"], files))
    return failures, passed


def self_test():
    problems = []

    def expect(name, ok):
        print("%s %s" % ("ok  " if ok else "FAIL", name))
        if not ok:
            problems.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        g2 = root / "ihp-sg13g2"
        (g2 / "models").mkdir(parents=True)
        g2.joinpath(*OSDI_DIR).mkdir(parents=True)
        (g2 / "models" / "a.lib").write_text("a")
        g2.joinpath(*OSDI_DIR, "p.osdi").write_text("p")
        h = lambda text: hashlib.sha256(text.encode()).hexdigest()
        context = {"tools": [{"name": "ngspice", "sha256": h("n")}, {"name": "iverilog", "sha256": h("i")}],
                   "vpi_modules": [{"name": "cocotb.vpl", "sha256": h("c")},
                                   {"name": "system.vpi", "sha256": h("s")}],
                   "python_sha256": h("py"), "libpython_sha256": h("lp"), "cocotb_version": "2.0.0",
                   "cocotb_package_sha256": h("pkg"), "spinit_sha256": h("spinit 1"),
                   "models": [{"pdk_name": "ihp-sg13g2", "sections": [],
                               "reached": [{"file": "models/a.lib", "sha256": h("a")}],
                               "osdi": [h("p")]}]}
        entry = {"id": "mixed.one", "kind": "mixed", "context": context}
        image = {key: json.loads(json.dumps(context[key])) for key in
                 ("tools", "vpi_modules", "python_sha256", "libpython_sha256", "cocotb_version",
                  "cocotb_package_sha256")}
        image["spinit"] = lambda n: h("spinit %d" % n)
        roots = {"ihp-sg13g2": str(g2)}

        class Fake:
            def __init__(self, entries, image):
                self.table, self.got = entries, image

            def entries(self):
                return self.table

            def roots(self, environ):
                return roots

            def image(self, environ, work):
                return self.got

        def run(entries=(entry,), got=None, min_entries=1):
            return check(Fake(list(entries), got or image), {}, root, min_entries)

        failures, passed = run()
        expect("the image's files pass", failures == [] and len(passed) == 1)
        expect("the pass names its counts", "2 tools, 2 VPI modules, cocotb 2.0.0, 2 model" in passed[0])

        moved = dict(image, tools=[{"name": "ngspice", "sha256": h("x")}, image["tools"][1]])
        failures, _ = run(got=moved)
        expect("a moved tool is named", failures == ["mixed.one: tools: ngspice is %s, the entry has %s"
                                                     % (h("x")[:8], h("n")[:8])])
        failures, _ = run(got=dict(image, vpi_modules=image["vpi_modules"][::-1]))
        expect("VPI order is held", len(failures) == 1 and "vpi: order" in failures[0])
        failures, _ = run(got=dict(image, vpi_modules=image["vpi_modules"][:1]))
        expect("a missing VPI module is named", len(failures) == 1 and "system.vpi is named" in failures[0])
        failures, _ = run(got=dict(image, cocotb_version="1.9.2"))
        expect("another cocotb is named", len(failures) == 1 and "cocotb_version" in failures[0])
        failures, _ = run(got=dict(image, spinit=lambda n: h("spinit 0")))
        expect("another spinit is named", len(failures) == 1 and "for 1 OSDI files" in failures[0])

        (g2 / "models" / "a.lib").write_text("b")
        failures, _ = run()
        expect("a moved model file is named", len(failures) == 1 and "models/a.lib is" in failures[0])
        (g2 / "models" / "a.lib").unlink()
        failures, _ = run()
        expect("a missing model file is named", len(failures) == 1 and "is not there" in failures[0])
        (g2 / "models" / "a.lib").write_text("a")
        g2.joinpath(*OSDI_DIR, "p.osdi").write_text("q")
        failures, _ = run()
        expect("a moved OSDI file is named", len(failures) == 1 and "OSDI" in failures[0])
        g2.joinpath(*OSDI_DIR, "p.osdi").write_text("p")

        gf = dict(entry, id="mixed.two", context=dict(context, models=context["models"] + [
            {"pdk_name": "gf180mcuD", "sections": [], "reached": [], "osdi": []}]))
        failures, _ = run(entries=(entry, gf))
        expect("a PDK without a root is named for its entry only",
               failures == ["mixed.two: models: no root is named for gf180mcuD"])
        failures, _ = run(min_entries=2)
        expect("too few entries is a failure", len(failures) == 1 and "fewer than 2" in failures[0])

    print("%d problem(s)" % len(problems))
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-entries", type=int, default=1,
                        help="fail when the table has fewer mixed entries than this")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    with tempfile.TemporaryDirectory() as tmp:
        failures, passed = check(ChipletCosim(), os.environ, pathlib.Path(tmp), args.min_entries)
    for line in passed:
        print("ok   %s" % line)
    for line in failures:
        print("FAIL %s" % line)
    print("cosim mixed entries: %d held, %d failure(s)" % (len(passed), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
