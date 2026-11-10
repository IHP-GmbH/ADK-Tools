"""The format checker and the formats/ directory it guards.

Run: python3 -B -m unittest discover -s tests/formats   (from the adk-tools root)

The records themselves are checked where they are written: tests/host_verify/test_host_verify.py for
adk-tools-host-verify-record/v1 and nix/test-pdk-source.sh for adk-tools-pdk-source/v1. This file
checks the checker, and that formats/ holds only what its README says it holds.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FORMATS = ROOT / "formats"
CHECKER = HERE / "check_format.py"
# sha256 of the Apache License 2.0 text as published at apache.org/licenses/LICENSE-2.0.txt
APACHE_2_0 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
NAME = re.compile(r"adk-tools-([a-z0-9]+(?:-[a-z0-9]+)*)\.v([1-9][0-9]*)\.schema\.json")

_spec = spec_from_loader("check_format", SourceFileLoader("check_format", str(CHECKER)))
cf = module_from_spec(_spec)
_spec.loader.exec_module(cf)


def obj(props: dict, **extra) -> dict:
    return {"type": "object", "properties": props, "required": sorted(props),
            "additionalProperties": False, **extra}


SAMPLE = obj({
    "format": {"type": "string", "const": "adk-tools-sample/v1"},
    "n": {"type": ["integer", "null"]},
    "kind": {"type": "string", "enum": ["a", "b"]},
    "id": {"type": "string", "pattern": "^[0-9a-f]{4}$"},
    "items": {"type": "array", "items": obj({"path": {"type": "string"}})},
})
GOOD = {"format": "adk-tools-sample/v1", "n": None, "kind": "a", "id": "00ff", "items": [{"path": "x"}]}


class Lint(unittest.TestCase):
    def refused(self, schema: dict, words: str) -> None:
        with self.assertRaises(cf.SchemaError) as ctx:
            cf.lint(schema)
        self.assertIn(words, str(ctx.exception))

    def test_the_sample_passes(self):
        cf.lint(SAMPLE)

    def test_a_keyword_outside_the_subset(self):
        self.refused(obj({"a": {"oneOf": [{"type": "string"}]}}), "does not read: oneOf")
        self.refused(obj({"a": {"type": "integer", "minimum": 0}}), "does not read: minimum")

    def test_root_only_keywords_below_the_root(self):
        self.refused(obj({"a": {"type": "string", "$comment": "x"}}), "does not read: $comment")

    def test_an_id_is_not_read(self):
        self.refused({**SAMPLE, "$id": "https://example.org/x"}, "does not read: $id")

    def test_an_open_object(self):
        self.refused({**SAMPLE, "additionalProperties": True}, "additionalProperties to false")
        nested = obj({"c": {"type": "object", "properties": {}, "required": []}})
        self.refused(nested, "additionalProperties to false")

    def test_required_must_be_every_property(self):
        self.refused({**SAMPLE, "required": ["format"]}, "required must list every property")

    def test_an_array_without_items(self):
        self.refused(obj({"a": {"type": "array"}}), "must say what its items are")

    def test_a_pattern_that_does_not_compile(self):
        self.refused(obj({"a": {"type": "string", "pattern": "("}}), "does not compile")

    def test_an_unknown_type(self):
        self.refused(obj({"a": {"type": "int"}}), "distinct JSON types")


class Check(unittest.TestCase):
    def test_a_conforming_record(self):
        self.assertEqual(cf.check(SAMPLE, GOOD), [])

    def test_an_extra_and_a_missing_key(self):
        rec = {k: v for k, v in GOOD.items() if k != "n"} | {"new": 1}
        self.assertEqual(cf.check(SAMPLE, rec), [
            "(root): key(s) the schema does not name: new",
            "(root): key(s) the schema requires but the record lacks: n"])

    def test_nested_keys(self):
        rec = GOOD | {"items": [{"path": "x"}, {"path": "y", "mode": "ro"}]}
        self.assertEqual(cf.check(SAMPLE, rec), ["/items/1: key(s) the schema does not name: mode"])

    def test_values(self):
        rec = GOOD | {"format": "adk-tools-sample/v2", "kind": "c", "id": "00FF"}
        self.assertEqual(len(cf.check(SAMPLE, rec)), 3)

    def test_a_trailing_dollar_is_the_end_of_the_string(self):
        rev = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
        self.assertEqual(cf.check(rev, "0" * 40), [])
        self.assertEqual(len(cf.check(rev, "0" * 40 + "\n")), 1)
        self.assertEqual(cf.check(SAMPLE, GOOD | {"id": "00ff\n"}),
                         ["/id: '00ff\\n' does not match '^[0-9a-f]{4}$'"])
        escaped = {"type": "string", "pattern": "^a\\$"}
        self.assertEqual((cf.check(escaped, "a$"), len(cf.check(escaped, "a"))), ([], 1))
        unanchored = {"type": "string", "pattern": "^[A-Z]+="}
        self.assertEqual(cf.check(unanchored, "HOME=/tmp\n"), [])

    def test_types(self):
        self.assertEqual(cf.check(SAMPLE, GOOD | {"n": True}),
                         ["/n: type boolean, the schema wants integer or null"])
        self.assertEqual(cf.check(SAMPLE, GOOD | {"n": "3"}),
                         ["/n: type string, the schema wants integer or null"])
        self.assertEqual(cf.check(SAMPLE, GOOD | {"n": 3}), [])


class Cli(unittest.TestCase):
    def run_cli(self, schema, record) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            s, r = Path(tmp, "s.json"), Path(tmp, "r.json")
            s.write_text(json.dumps(schema))
            r.write_text(json.dumps(record))
            return subprocess.run([sys.executable, "-B", str(CHECKER), str(s), str(r)],
                                  capture_output=True, text=True, timeout=30)

    def test_exit_codes(self):
        self.assertEqual(self.run_cli(SAMPLE, GOOD).returncode, 0)
        bad = self.run_cli(SAMPLE, GOOD | {"n": "x"})
        self.assertEqual((bad.returncode, bad.stdout.count("\n")), (1, 1))
        self.assertEqual(self.run_cli({**SAMPLE, "$id": "x"}, GOOD).returncode, 2)


class FormatsDirectory(unittest.TestCase):
    def schemas(self) -> list[Path]:
        return sorted(FORMATS.glob("*.schema.json"))

    def test_only_the_license_the_readme_and_schemas(self):
        others = sorted(p.name for p in FORMATS.iterdir()
                        if p.name not in ("LICENSE", "README.md") and not p.name.endswith(".schema.json"))
        self.assertEqual(others, [])
        self.assertTrue(self.schemas())

    def test_the_license_is_the_apache_2_0_text(self):
        self.assertEqual(hashlib.sha256((FORMATS / "LICENSE").read_bytes()).hexdigest(), APACHE_2_0)

    def test_each_schema(self):
        for path in self.schemas():
            with self.subTest(schema=path.name):
                m = NAME.fullmatch(path.name)
                self.assertIsNotNone(m, "file name is not adk-tools-<name>.v<N>.schema.json")
                schema = json.loads(path.read_text())
                cf.lint(schema)
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertIn("SPDX-License-Identifier: Apache-2.0", schema["$comment"])
                self.assertEqual(schema["properties"]["format"],
                                 {**schema["properties"]["format"],
                                  "const": f"adk-tools-{m[1]}/v{m[2]}"})

    def test_the_readme_names_each_schema_and_no_other(self):
        text = (FORMATS / "README.md").read_text()
        named = set(re.findall(r"adk-tools-[a-z0-9-]+\.v[0-9]+\.schema\.json", text))
        self.assertEqual(named, {p.name for p in self.schemas()})
        self.assertIn("SPDX-License-Identifier: Apache-2.0", text)


if __name__ == "__main__":
    unittest.main()
