"""adk-verify-carrier-drc reads the pillar manifest --pillars names, else the
one beside the GDS, and delegates the bump-to-bump rules from it alone.

A verify run places its inputs under names of its own, so the manifest need
not sit at <gds stem>.pillars.json; the chain's argv names no --pillars and
keeps reading the file beside the GDS.
"""

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "bin" / "adk-verify-carrier-drc"


def load():
    loader = SourceFileLoader("adk_verify_carrier_drc", str(SCRIPT))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


class PillarsFlag(unittest.TestCase):

    def setUp(self):
        self.drc = load()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.gds = base / "out" / "carrier.gds"
        self.gds.parent.mkdir()
        self.pdk = base / "pdk"
        write(self.pdk / "libs.tech" / "klayout" / "tech" / "drc" / "rule_decks"
              / "interposer_tech_default.json", {"rules": {"Padc_b": 25, "Padc_e": 50}})
        self.ixn = base / "ixn"
        write(self.ixn / "manifest" / "interconnect_methods.json", {"methods": {
            "fine": {"pitch_rules": {"IXN_spacing": 10, "IXN_pitch": 20}}}})
        self.manifest = {"pillars": [{"method": "fine"}]}

    def rules(self, pillars=None):
        return self.drc.delegated_bump_rules(self.gds, self.pdk, self.ixn, pillars)

    def test_the_default_reads_the_manifest_beside_the_gds(self):
        self.assertEqual(self.rules(), (set(), "no pillar manifest beside the GDS "
                                               "(carrier.pillars.json)"))
        write(self.gds.with_suffix(".pillars.json"), self.manifest)
        self.assertEqual(self.rules()[0], {"Padc.b", "Padc.e"})

    def test_the_flag_reads_its_file_and_not_the_one_beside_the_gds(self):
        write(self.gds.with_suffix(".pillars.json"), {"pillars": []})
        placed = write(self.gds.parent.parent / "inputs" / "pillars", self.manifest)
        self.assertEqual(self.rules(placed)[0], {"Padc.b", "Padc.e"})
        self.assertEqual(self.rules(), (set(), "carrier.pillars.json records no "
                                               "attachment method"))

    def test_a_flag_naming_no_file_delegates_nothing(self):
        write(self.gds.with_suffix(".pillars.json"), self.manifest)
        missing = self.gds.parent / "absent.json"
        self.assertEqual(self.rules(missing), (set(), f"no pillar manifest at {missing}"))

    def test_the_cli_names_the_flag(self):
        source = SCRIPT.read_text()
        self.assertIn('ap.add_argument("--pillars", type=Path,', source)
        self.assertIn("args.interconnect_root, args.pillars)", source)


if __name__ == "__main__":
    unittest.main()
