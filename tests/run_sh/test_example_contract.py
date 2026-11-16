"""The two-die example's hand-written die contract names the bytes beside it."""

import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "examples" / "two_die_interposer" / "chiplets" / "metal_test_contract" / "die-contract.json"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExampleContractTest(unittest.TestCase):
    def test_the_contract_digests_are_the_files_it_names(self):
        contract = json.loads(CONTRACT.read_text())
        here = CONTRACT.parent
        die, inventory = contract["die"], contract["inventory"]
        self.assertEqual(sha256(here / die["layout"]), die["layout_sha256"],
                         "Metal_Test.gds changed: regenerate the contract's layout_sha256 and box")
        self.assertEqual(sha256(here / inventory["path"]), inventory["sha256"],
                         "the pin list changed: regenerate the contract's inventory sha256")

    def test_the_contract_says_it_is_partial(self):
        contract = json.loads(CONTRACT.read_text())
        self.assertTrue(contract["generator"].startswith("hand-written"), contract.get("generator"))


if __name__ == "__main__":
    unittest.main()
