"""adk-verify-carrier-drc --margins-dir: the PDK's drc_margins.py after the deck.

Without the flag the gate prints and returns what it did before. With it, the
tool runs once, under the deck's Python with one thread, its output goes to
DIR/margins.log, and one "margins :" line says where the report is or why there
is none. The exit status stays the deck's verdict whatever the tool does.
"""

import contextlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "bin" / "adk-verify-carrier-drc"

# The deck runner: one report with RUN_DRC_RULES' markers (comma separated),
# the deck's completion line, and the runner's summary; exit 1 when it found any.
RUN_DRC = '''import os, sys
from pathlib import Path
args = sys.argv[1:]
run_dir = Path(args[args.index("--run_dir") + 1])
rules = [r for r in os.environ.get("RUN_DRC_RULES", "").split(",") if r]
items = "".join("<item><category>'%s'</category></item>" % r for r in rules)
(run_dir / "deck.lyrdb").write_text("<report-database><items>%s</items></report-database>" % items)
print("DRC check complete for interposer backend layers")
print("Violated rules: %r" % sorted(set(rules)))
sys.exit(1 if rules else 0)
'''

# drc_margins.py: records its argv, says something on each stream, writes the
# report on exit 0, else exits MARGINS_EXIT with its cause on stderr.
DRC_MARGINS = '''import json, os, sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index("-o") + 1])
(out / "argv.json").write_text(json.dumps(sys.argv[1:]))
print("margins: measuring")
code = int(os.environ.get("MARGINS_EXIT", "0"))
if code:
    sys.stderr.write("margins: cannot read the layer map\\\\n")
    sys.exit(code)
(out / "margins.json").write_text("{}")
(out / "margins.txt").write_text("all rules: margin >= 0\\\\n")
'''


def load():
    loader = SourceFileLoader("adk_verify_carrier_drc_margins", str(SCRIPT))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class MarginsFlag(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.pdk = self.base / "pdk"
        drc = self.pdk / "libs.tech" / "klayout" / "tech" / "drc"
        drc.mkdir(parents=True)
        (drc / "run_drc.py").write_text(RUN_DRC)
        self.tool = self.pdk / "libs.tech" / "klayout" / "python" / "drc_margins.py"
        self.tool.parent.mkdir(parents=True)
        self.tool.write_text(DRC_MARGINS)
        self.gds = self.base / "carrier.gds"
        self.gds.write_text("")
        self.baseline = self.base / "baseline.json"
        self.baseline.write_text(json.dumps({"expected": {"M1.a": {"count": 1}}}))
        # The gate wants a klayout on PATH; the deck runner here needs none.
        bindir = self.base / "bin"
        bindir.mkdir()
        (bindir / "klayout").write_text("#!/bin/sh\n")
        (bindir / "klayout").chmod(0o755)
        self.saved = {k: os.environ.get(k) for k in ("PATH", "RUN_DRC_RULES", "MARGINS_EXIT")}
        self.addCleanup(self.restore)
        os.environ["PATH"] = "%s:%s" % (bindir, os.environ.get("PATH", "/usr/bin:/bin"))
        os.environ["RUN_DRC_RULES"] = "M1.a"
        os.environ.pop("MARGINS_EXIT", None)
        self.drc = load()
        self.drc.PY = sys.executable
        self.runs = 0

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def gate(self, *extra):
        self.runs += 1
        run_dir = self.base / ("run%d" % self.runs)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.drc.main(["--gds", str(self.gds), "--baseline", str(self.baseline),
                                  "--pdk-root", str(self.pdk), "--interconnect-root",
                                  str(self.base / "ixn"), "--run-dir", str(run_dir), *extra])
        # The run dir differs per run; the rest of the output is compared as is.
        return code, out.getvalue().replace(str(run_dir), "<run>")

    def margins_lines(self, text):
        return [line for line in text.splitlines() if line.startswith("  margins :")]

    def test_without_the_flag_nothing_changes(self):
        code, text = self.gate()
        self.assertEqual(code, 0)
        self.assertEqual(self.margins_lines(text), [])
        self.assertNotIn("margins", text)
        self.assertEqual(text.splitlines()[:4], [
            "carrier DRC on %s" % self.gds,
            "  deck    : %s" % (self.pdk / "libs.tech/klayout/tech/drc/run_drc.py"),
            "  reports : <run>/deck.lyrdb",
            "  total   : 1 marker(s) across 1 rule(s)"])
        self.assertEqual(text.splitlines()[-1], "carrier DRC OK: no rule exceeds its baseline.")

    def test_the_report_and_its_line(self):
        margins = self.base / "outputs" / "margins"
        code, text = self.gate("--margins-dir", str(margins))
        self.assertEqual(code, 0)
        self.assertEqual(self.margins_lines(text), ["  margins : %s" % (margins / "margins.txt")])
        lines = text.splitlines()
        self.assertEqual(lines.index("  margins : %s" % (margins / "margins.txt")), 4)
        self.assertEqual(json.loads((margins / "argv.json").read_text()),
                         [str(self.gds), "-o", str(margins), "--topcell", "INTERPOSER",
                          "--threads", "1"])
        self.assertTrue((margins / "margins.json").exists())
        self.assertIn("margins: measuring", (margins / "margins.log").read_text())
        # Everything but the one line is what the gate prints without the flag.
        _, plain = self.gate()
        self.assertEqual([l for l in lines if not l.startswith("  margins :")],
                         plain.splitlines())

    def test_a_failing_tool_is_named_and_the_deck_stays_the_verdict(self):
        os.environ["MARGINS_EXIT"] = "1"
        for rules, verdict in (("M1.a", 0), ("M1.a,M1.a,V1.b", 1)):
            with self.subTest(rules=rules):
                os.environ["RUN_DRC_RULES"] = rules
                margins = self.base / ("margins-%d" % verdict)
                code, text = self.gate("--margins-dir", str(margins))
                self.assertEqual(code, verdict)
                self.assertEqual(self.margins_lines(text), [
                    "  margins : not written, drc_margins.py exited 1 (see %s)"
                    % (margins / "margins.log")])
                log = (margins / "margins.log").read_text()
                self.assertIn("margins: cannot read the layer map", log)
                self.assertIn("margins: measuring", log)
                self.assertFalse((margins / "margins.txt").exists())

    def test_a_pdk_without_the_tool(self):
        self.tool.unlink()
        margins = self.base / "margins"
        code, text = self.gate("--margins-dir", str(margins))
        self.assertEqual(code, 0)
        why = "not written, the PDK has no %s" % self.tool
        self.assertEqual(self.margins_lines(text), ["  margins : " + why])
        self.assertEqual((margins / "margins.log").read_text(), why + "\n")


if __name__ == "__main__":
    unittest.main()
