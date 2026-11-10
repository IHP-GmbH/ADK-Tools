#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for nix/tool_identity.py: the NAR serialiser, the digests and the table.

The serialiser is checked against NAR bytes written out here from the format's
rules, and against nix-store --dump when this host has Nix. The table is built
from a stand-in store and a stub `nix` that answers path-info, the lane's from a
stand-in closureInfo, and each is checked against its schema with
tests/formats/check_format.py.

Usage: python3 nix/test_tool_identity.py
"""

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tests" / "formats"))
import check_format as cf  # noqa: E402
import tool_identity as ti  # noqa: E402
from test_tracked_tree import clean_repo  # noqa: E402

SCHEMA = HERE.parent / "formats" / "adk-tools-tool-identity.v1.schema.json"


def s(data: bytes) -> bytes:
    """A NAR string: its length as 8 bytes little-endian, the bytes, zeros to a multiple of 8."""
    return len(data).to_bytes(8, "little") + data + b"\0" * (-len(data) % 8)


def regular(contents: bytes, executable=False) -> bytes:
    return (s(b"(") + s(b"type") + s(b"regular")
            + (s(b"executable") + s(b"") if executable else b"")
            + s(b"contents") + s(contents) + s(b")"))


def nar(node: bytes) -> str:
    return hashlib.sha256(s(b"nix-archive-1") + node).hexdigest()


class Serialiser(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tool identity ")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def tree(self) -> Path:
        """A directory with an executable, a plain file, a symlink and a subdirectory."""
        top = self.base / "tree"
        (top / "sub").mkdir(parents=True)
        (top / "a").write_bytes(b"hello\n")
        (top / "B").write_bytes(b"")
        os.chmod(top / "B", 0o755)
        (top / "sub" / "é").write_bytes(b"x" * 9)
        os.symlink("../a", top / "sub" / "link")
        return top

    def tree_nar(self) -> bytes:
        # Entries in byte order of their names: "B" (0x42) before "a" (0x61),
        # "link" (0x6c) before "é" (0xc3 0xa9).
        def entry(name, node):
            return s(b"entry") + s(b"(") + s(b"name") + s(name) + s(b"node") + node + s(b")")
        sub = (s(b"(") + s(b"type") + s(b"directory")
               + entry(b"link", s(b"(") + s(b"type") + s(b"symlink") + s(b"target")
                       + s(b"../a") + s(b")"))
               + entry("é".encode(), regular(b"x" * 9))
               + s(b")"))
        return (s(b"(") + s(b"type") + s(b"directory")
                + entry(b"B", regular(b"", executable=True))
                + entry(b"a", regular(b"hello\n"))
                + entry(b"sub", sub)
                + s(b")"))

    def test_a_file(self):
        f = self.base / "f"
        f.write_bytes(b"hi")
        os.chmod(f, 0o644)
        self.assertEqual(ti.nar_sha256(f), nar(regular(b"hi")))

    def test_only_the_owner_bit_makes_a_file_executable(self):
        f = self.base / "f"
        f.write_bytes(b"#!/bin/sh\n")
        for mode, executable in ((0o755, True), (0o700, True), (0o644, False),
                                 (0o654, False), (0o645, False)):
            with self.subTest(mode=oct(mode)):
                os.chmod(f, mode)
                self.assertEqual(ti.nar_sha256(f), nar(regular(b"#!/bin/sh\n", executable)))

    def test_a_tree(self):
        self.assertEqual(ti.nar_sha256(self.tree()), nar(self.tree_nar()))

    def test_a_symlink_is_not_followed(self):
        link = self.base / "link"
        os.symlink("/nonexistent/target", link)
        self.assertEqual(ti.nar_sha256(link), nar(s(b"(") + s(b"type") + s(b"symlink")
                                                  + s(b"target") + s(b"/nonexistent/target")
                                                  + s(b")")))

    def test_a_socket_is_refused(self):
        path = self.base / "sock"
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(path))
        with self.assertRaisesRegex(OSError, "NAR has no form for it"):
            ti.nar_sha256(path)

    def test_equals_nix_store_dump(self):
        nix_store = shutil.which("nix-store")
        if not nix_store:
            self.skipTest("no nix-store on this host; the golden tests above still ran")
        top = self.tree()
        dump = subprocess.run([nix_store, "--dump", str(top)], check=True,
                              capture_output=True).stdout
        self.assertEqual(dump, s(b"nix-archive-1") + self.tree_nar())
        self.assertEqual(ti.nar_sha256(top), hashlib.sha256(dump).hexdigest())


class Digests(unittest.TestCase):

    def test_sri_to_hex(self):
        digest = hashlib.sha256(b"x").digest()
        self.assertEqual(ti.sri_to_hex("sha256-" + base64.b64encode(digest).decode()),
                         digest.hex())
        for bad in ("sha256:" + digest.hex(), "sha512-" + base64.b64encode(digest).decode(),
                    "sha256-abc="):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ti.sri_to_hex(bad)

    def test_the_closure_digest_is_over_sorted_contents(self):
        a, b = "a" * 64, "b" * 64
        want = hashlib.sha256(f"{a}\n{b}\n".encode()).hexdigest()
        self.assertEqual(ti.closure_sha256([b, a]), want)
        self.assertEqual(ti.closure_sha256({"/x/2-b": b, "/x/1-a": a}.values()), want)
        # A path is a line: two paths of the same content count twice.
        self.assertNotEqual(ti.closure_sha256([a, a, b]), want)


class Store(unittest.TestCase):
    """A stand-in store of three paths, two pins, a closure list and a stub `nix`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tool identity ")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = self.base / "store"
        self.paths = {}
        for name, hash_part in (("engine", "a" * 32), ("lib", "b" * 32), ("router", "c" * 32)):
            path = self.store / f"{hash_part}-{name}"
            (path / "bin").mkdir(parents=True)
            (path / "bin" / name).write_text(f"#!/bin/sh\necho {name}\n")
            self.paths[name] = str(path)
        self.pin = self.base / "staging.pin"
        self.pin.write_text(
            "# stand-in pins\n"
            f'NIX_MY_ENGINE_OUTPATH="{self.paths["engine"]}"\n'
            f'NIX_ROUTER_OUTPATH="{self.paths["router"]}"\n'
            'NIX_ROUTER_REV="not a path pin"\n')
        listing = self.base / "closure.txt"
        listing.write_text(f'# the image store\n{self.paths["engine"]}\n{self.paths["lib"]}\n')
        patcher = mock.patch.object(ti, "IMAGE_CLOSURES", (listing,))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The engine needs the lib; the router needs nothing.
        self.closures = {self.paths["engine"]: [self.paths["engine"], self.paths["lib"]],
                         self.paths["lib"]: [self.paths["lib"]],
                         self.paths["router"]: [self.paths["router"]]}
        self.lie = {}
        stub = self.base / "bin" / "nix"
        stub.parent.mkdir()
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "assert sys.argv[1:4] == ['path-info', '--json', '--recursive'], sys.argv\n"
            f"answers = json.load(open({str(self.base / 'answers.json')!r}))\n"
            "print(json.dumps(answers[sys.argv[4]]))\n")
        stub.chmod(0o755)
        env = mock.patch.dict(os.environ, {"PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}"})
        env.start()
        self.addCleanup(env.stop)

    def answer(self):
        """What nix path-info --json --recursive says for each path, the narHash ours or a lie."""
        def sri(path):
            return "sha256-" + base64.b64encode(bytes.fromhex(
                self.lie.get(path) or ti.nar_sha256(path))).decode()
        answers = {p: {r: {"narHash": sri(r), "references": []} for r in req}
                   for p, req in self.closures.items()}
        (self.base / "answers.json").write_text(json.dumps(answers))


class Table(Store):

    def test_the_table(self):
        self.answer()
        data, checked = ti.host_table(self.pin, True, [])
        self.assertTrue(data.endswith(b"}\n") and data.count(b"\n") == 1)
        record = json.loads(data)
        self.assertEqual(cf.check(json.loads(SCHEMA.read_text()), record), [])
        nar = {p: ti.nar_sha256(p) for p in self.paths.values()}
        self.assertEqual(record, {
            "format": "adk-tools-tool-identity/v1",
            "closure_sha256": ti.closure_sha256(nar.values()),
            "tools": [
                {"name": "my-engine", "from": "image-store", "path": self.paths["engine"],
                 "nar_sha256": nar[self.paths["engine"]],
                 "closure_sha256": ti.closure_sha256([nar[self.paths["engine"]],
                                                      nar[self.paths["lib"]]]),
                 "version": None, "binary": None, "binary_sha256": None},
                {"name": "router", "from": "host-store", "path": self.paths["router"],
                 "nar_sha256": nar[self.paths["router"]],
                 "closure_sha256": ti.closure_sha256([nar[self.paths["router"]]]),
                 "version": None, "binary": None, "binary_sha256": None}]})
        # Every path of the image's store, serialised.
        self.assertEqual(checked, sorted(f"{nar[p]}  {p}" for p in
                                         (self.paths["engine"], self.paths["lib"])))
        self.assertEqual(data, json.dumps(record, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode() + b"\n")

    def test_a_narhash_the_serialiser_disagrees_with_is_refused(self):
        self.lie[self.paths["lib"]] = "0" * 64
        self.answer()
        with self.assertRaisesRegex(ValueError, "serialised NAR sha256 .*, nix narHash 0{64}"):
            ti.host_table(self.pin, True, [])
        # Without the check the host's word is taken.
        ti.host_table(self.pin, False, [])

    def test_a_tool_outside_nix(self):
        self.answer()
        tree = self.base / "opt" / "tool"
        tree.mkdir(parents=True)
        (tree / "tool").write_bytes(b"\x7fELF")
        os.symlink("tool", tree / "tool-link")
        data, _ = ti.host_table(self.pin, False, [f"tool=Tool 1.2:{tree / 'tool-link'}:{tree}",
                                                  f"bare=v3:{tree / 'tool'}"])
        record = json.loads(data)
        self.assertEqual(cf.check(json.loads(SCHEMA.read_text()), record), [])
        tools = {t["name"]: t for t in record["tools"]}
        self.assertEqual(tools["tool"], {
            "name": "tool", "from": "image", "path": str(tree), "nar_sha256": ti.nar_sha256(tree),
            "closure_sha256": None, "version": "Tool 1.2", "binary": str(tree / "tool"),
            "binary_sha256": hashlib.sha256(b"\x7fELF").hexdigest()})
        self.assertEqual((tools["bare"]["path"], tools["bare"]["nar_sha256"]), (None, None))
        self.assertEqual([t["name"] for t in record["tools"]],
                         ["bare", "my-engine", "router", "tool"])

    def test_a_tracked_tool(self):
        self.answer()
        repo = clean_repo(self.base / "checkout")
        script = self.base / "venv" / "tool"
        script.parent.mkdir()
        script.write_text("#!/venv/bin/python\n")
        spec = f"tool={script}:{repo}"
        data, _ = ti.host_table(self.pin, False, [], [spec])
        record = json.loads(data)
        self.assertEqual(cf.check(json.loads(SCHEMA.read_text()), record), [])
        row = {t["name"]: t for t in record["tools"]}["tool"]
        self.assertEqual(row, ti.tracked_entry("tool", str(script), str(repo)))
        self.assertRegex(row["version"], r"^commit [0-9a-f]{40} tracked [0-9a-f]{64}$")
        out = self.base / "tracked.json"
        cli = [sys.executable, "-B", str(HERE / "tool_identity.py"), "host", "--pin", str(self.pin),
               "--tracked-tool", spec, "--out", str(out)]
        made = subprocess.run(cli, capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual({t["name"]: t for t in json.loads(out.read_text())["tools"]}["tool"], row)
        # A tree with no tracked digest writes no table.
        out.unlink()
        (repo / "notes.txt").write_text("not added\n")
        with self.assertRaisesRegex(ValueError, "^tool: .*: no tracked digest: notes.txt is not "
                                                "tracked$"):
            ti.host_table(self.pin, False, [], [spec])
        refused = subprocess.run(cli, capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual((refused.returncode, refused.stdout, out.exists()), (1, "", False))
        self.assertEqual(refused.stderr, f"tool_identity: tool: {repo}: no tracked digest: "
                                         f"notes.txt is not tracked\n")

    def test_a_repeated_name_is_refused(self):
        self.answer()
        with self.assertRaisesRegex(ValueError, "tool names repeat"):
            ti.host_table(self.pin, False, [f"router=1:{self.paths['router']}/bin/router"])

    def test_the_cli(self):
        self.answer()
        out = self.base / "tool-identity.json"
        result = subprocess.run([sys.executable, "-B", str(HERE / "tool_identity.py"), "host",
                                 "--pin", str(self.pin), "--out", str(out)],
                                capture_output=True, text=True, check=False,
                                env=dict(os.environ))
        # The stand-in closure list is patched in this process only, so the
        # child takes the real lists and finds neither pin in them.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         f"{hashlib.sha256(out.read_bytes()).hexdigest()}  {out}\n")
        self.assertEqual({t["from"] for t in json.loads(out.read_text())["tools"]},
                         {"host-store"})
        bad = subprocess.run([sys.executable, "-B", str(HERE / "tool_identity.py"), "nar",
                              str(self.base / "missing")], capture_output=True, text=True)
        self.assertEqual((bad.returncode, bad.stdout), (1, ""))
        self.assertTrue(bad.stderr.startswith("tool_identity: "), bad.stderr)


def nix32(digest: bytes) -> str:
    """Nix's base32 of a 32-byte digest, written out from its rule: the last bits first."""
    alphabet = "0123456789abcdfghijklmnpqrsvwxyz"
    out = []
    for n in range(51, -1, -1):
        i, j = divmod(n * 5, 8)
        c = (digest[i] >> j) | ((digest[i + 1] << (8 - j)) if i + 1 < 32 else 0)
        out.append(alphabet[c & 0x1F])
    return "".join(out)


class LaneTable(Store):
    """`lane`: the table from a closureInfo, with no store query."""

    def setUp(self):
        super().setUp()
        # Store paths by name only: nothing here reads their bytes.
        self.paths = {name: f"/nix/store/{h * 32}-{name}"
                      for name, h in (("engine", "a"), ("lib", "b"), ("router", "c"))}
        self.pin.write_text(f'NIX_MY_ENGINE_OUTPATH="{self.paths["engine"]}"\n'
                            f'NIX_ROUTER_OUTPATH="{self.paths["router"]}"\n')
        self.closures = {self.paths["engine"]: [self.paths["engine"], self.paths["lib"]],
                         self.paths["lib"]: [self.paths["lib"]],
                         self.paths["router"]: [self.paths["router"]]}
        self.nar = {p: hashlib.sha256(p.encode()).hexdigest() for p in self.paths.values()}
        # The lane's closure holds the engine and its lib, not the router.
        self.info = self.base / "closure-info"
        self.info.mkdir()
        self.write_info([(self.paths["engine"], [self.paths["lib"], self.paths["engine"]]),
                         (self.paths["lib"], [])])

    def write_info(self, records, hex_for=(), listed=None):
        lines = []
        for path, refs in records:
            digest = bytes.fromhex(self.nar[path])
            form = digest.hex() if path in hex_for else nix32(digest)
            lines += [path, f"sha256:{form}", "1024", "", str(len(refs)), *refs]
        (self.info / "registration").write_text("\n".join(lines) + "\n")
        listed = [path for path, _ in records] if listed is None else listed
        (self.info / "store-paths").write_text("".join(f"{p}\n" for p in listed))

    def test_nix32_as_a_store_records_it(self):
        # A closureInfo's line and `nix path-info`'s narHash for one path, as hex.
        self.assertEqual(ti.nix32_to_hex("05hz16anly4ymph08x29rm1q5cb96rd6m7sv4v2z3ycl7svl993c"),
                         "6ca444b73e94f9f1c5265b9f6a5a3669b18243cd497404e0ad9e786a95091f16")
        for digest in (bytes(32), b"\xff" * 32, hashlib.sha256(b"x").digest()):
            self.assertEqual(ti.nix32_to_hex(nix32(digest)), digest.hex())
        for bad in ("0" * 51, "0" * 53, "e" + "0" * 51, "2" + "0" * 51):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ti.nix32_to_hex(bad)

    def test_the_lane_table(self):
        with mock.patch.object(ti.subprocess, "run", side_effect=AssertionError("a store query")):
            data = ti.lane_table(self.info, self.pin, [])
        record = json.loads(data)
        self.assertEqual(cf.check(json.loads(SCHEMA.read_text()), record), [])
        engine, lib = self.nar[self.paths["engine"]], self.nar[self.paths["lib"]]
        self.assertEqual(record, {
            "format": "adk-tools-tool-identity/v1",
            "closure_sha256": ti.closure_sha256([engine, lib]),
            "tools": [
                {"name": "my-engine", "from": "host-store", "path": self.paths["engine"],
                 "nar_sha256": engine, "closure_sha256": ti.closure_sha256([engine, lib]),
                 "version": None, "binary": None, "binary_sha256": None}]})

    def test_a_pin_reads_as_the_host_reads_it(self):
        self.write_info([(self.paths["engine"], [self.paths["lib"]]), (self.paths["lib"], []),
                         (self.paths["router"], [])], hex_for=(self.paths["lib"],))
        self.lie = {p: self.nar[p] for p in self.paths.values()}
        self.answer()
        host, _ = ti.host_table(self.pin, False, [])
        lane = ti.lane_table(self.info, self.pin, [])
        fields = ("name", "path", "nar_sha256", "closure_sha256")
        self.assertEqual([[t[f] for f in fields] for t in json.loads(lane)["tools"]],
                         [[t[f] for f in fields] for t in json.loads(host)["tools"]])

    def test_the_tools_no_pin_names(self):
        repo = clean_repo(self.base / "checkout")
        script = self.base / "venv" / "tool"
        script.parent.mkdir()
        script.write_text("#!/venv/bin/python\n")
        git = self.base / "git"
        git.write_bytes(b"\x7fELF")
        record = json.loads(ti.lane_table(self.info, self.pin, [f"git=git version 2:{git}"],
                                          [f"tool={script}:{repo}"]))
        self.assertEqual(cf.check(json.loads(SCHEMA.read_text()), record), [])
        tools = {t["name"]: t for t in record["tools"]}
        self.assertEqual(sorted(tools), ["git", "my-engine", "tool"])
        self.assertEqual(tools["git"], ti.image_entry("git", "git version 2", str(git), None))
        self.assertEqual(tools["tool"], ti.tracked_entry("tool", str(script), str(repo)))

    def test_a_closure_info_that_does_not_fit_is_refused(self):
        engine, lib, router = self.paths["engine"], self.paths["lib"], self.paths["router"]
        reg = self.info / "registration"
        cases = {
            "truncated record": lambda: reg.write_text(reg.read_text() + f"{router}\n"),
            "bad size or reference count": lambda: reg.write_text(
                reg.read_text().replace("\n1024\n", "\nmany\n", 1)),
            "truncated references": lambda: reg.write_text(
                reg.read_text().replace(f"\n0\n", "\n2\n").rstrip("\n") + "\n"),
            "is referenced and not registered": lambda: self.write_info(
                [(engine, [lib]), (lib, [router])]),
            "store-paths and the registration differ at": lambda: self.write_info(
                [(engine, [lib]), (lib, [])], listed=[engine]),
            "is not a new store path": lambda: self.write_info(
                [(engine, [lib]), (lib, []), (lib, [])]),
            "not a sha256 narHash": lambda: reg.write_text(
                reg.read_text().replace("sha256:", "sha512:", 1)),
        }
        for why, break_it in cases.items():
            with self.subTest(why=why):
                self.write_info([(engine, [lib]), (lib, [])])
                break_it()
                with self.assertRaisesRegex(ValueError, why):
                    ti.lane_table(self.info, self.pin, [])

    def test_the_cli(self):
        out = self.base / "lane.json"
        cli = [sys.executable, "-B", str(HERE / "tool_identity.py"), "lane",
               "--closure-info", str(self.info), "--pin", str(self.pin), "--out", str(out)]
        made = subprocess.run(cli, capture_output=True, text=True, env={"PATH": "/nonexistent"})
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(made.stdout, f"{hashlib.sha256(out.read_bytes()).hexdigest()}  {out}\n")
        self.assertEqual(out.read_bytes(), ti.lane_table(self.info, self.pin, []))
        out.unlink()
        (self.info / "store-paths").unlink()
        refused = subprocess.run(cli, capture_output=True, text=True)
        self.assertEqual((refused.returncode, refused.stdout, out.exists()), (1, "", False))
        self.assertTrue(refused.stderr.startswith("tool_identity: "), refused.stderr)


class ImageTable(Store):
    """The image build's two steps: `facts` on the host, `image` and `verify` in the image.

    The stand-in pins gain the Nix lane's python, which the image's own python
    row takes the name of."""

    def setUp(self):
        super().setUp()
        python = self.store / f"{'d' * 32}-python3-3.13.9-env"
        (python / "bin").mkdir(parents=True)
        (python / "bin" / "python3").write_text("#!/bin/sh\n")
        self.paths["python"] = str(python)
        self.closures[str(python)] = [str(python)]
        self.lane_pin = self.pin.read_text()
        self.pin.write_text(self.lane_pin + f'NIX_PYTHON_OUTPATH="{python}"\n')
        self.answer()
        opt = self.base / "opt"
        self.tools = []
        for name in ti.IMAGE_TOOLS:
            tree = opt / name
            tree.mkdir(parents=True)
            (tree / name).write_bytes(name.encode())
            self.tools.append(f"{name}={name} 1.0:{tree / name}"
                              + (f":{tree}" if name not in ("git", "python") else ""))
        self.facts = ti.predict(self.pin, ti.image_drop(ti.pins(self.pin)))

    def in_the_image(self):
        """The image's store: what the closure lists name, so not the host's router.
        The lane's python stays, as a path the image lacks would: the image passes
        over that pin before it looks."""
        shutil.rmtree(self.paths["router"])

    def test_the_facts(self):
        nar = {p: ti.nar_sha256(p) for p in self.paths.values()}
        self.assertEqual(self.facts, {
            "my-engine": {"path": self.paths["engine"], "from": "image-store",
                          "requisites": {self.paths["engine"]: nar[self.paths["engine"]],
                                         self.paths["lib"]: nar[self.paths["lib"]]}},
            "router": {"path": self.paths["router"], "from": "host-store",
                       "requisites": {self.paths["router"]: nar[self.paths["router"]]}}})

    def test_the_facts_leave_out_the_pin_an_image_tool_takes(self):
        self.assertNotIn("python", self.facts)
        self.assertIn("python", ti.predict(self.pin, frozenset()))

    def test_the_image_drop_is_exactly_the_names_pins_take(self):
        self.assertEqual(ti.image_drop(["python", "router"]), frozenset({"python"}))
        for names in ([], ["router"], ["python", "git"], ["kicad-cli"]):
            with self.subTest(names=names):
                with self.assertRaisesRegex(ValueError, "the pins named like an image tool are "
                                                        r".*; the image leaves out exactly "
                                                        r"\['python'\]"):
                    ti.image_drop(names)

    def test_the_host_keeps_the_lane_python(self):
        others = [t for t in self.tools if not t.startswith("python=")]
        data, _ = ti.host_table(self.pin, False, others)
        python = {t["name"]: t for t in json.loads(data)["tools"]}["python"]
        self.assertEqual((python["from"], python["path"]), ("host-store", self.paths["python"]))
        # Without the drop the image's python repeats the pin's name.
        with self.assertRaisesRegex(ValueError, "tool names repeat"):
            ti.host_table(self.pin, False, self.tools)

    def test_the_image_passes_over_a_dropped_pin_in_its_facts(self):
        undropped = ti.predict(self.pin, frozenset())
        self.in_the_image()
        self.assertEqual(ti.image_table(undropped, self.tools), ti.image_table(self.facts, self.tools))

    def test_the_image_writes_what_the_host_predicts(self):
        # The host's table for the image's pins: the lane's python is not one.
        self.pin.write_text(self.lane_pin)
        host, _ = ti.host_table(self.pin, False, self.tools)
        self.in_the_image()
        data, count = ti.image_table(self.facts, self.tools)
        self.assertEqual(data, host)
        self.assertEqual(count, 2)
        self.assertEqual(cf.check(json.loads(SCHEMA.read_text()), json.loads(data)), [])

    def test_a_store_path_other_than_the_hosts_is_refused(self):
        self.in_the_image()
        (Path(self.paths["lib"]) / "bin" / "lib").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "my-engine: .*-lib serialises to NAR sha256 "
                                                "[0-9a-f]{64}, the build host's narHash is"):
            ti.image_table(self.facts, self.tools)

    def test_a_store_path_the_image_lacks_is_refused(self):
        self.in_the_image()
        shutil.rmtree(self.paths["lib"])
        with self.assertRaisesRegex(ValueError, "my-engine: .*-lib is not in this image's store"):
            ti.image_table(self.facts, self.tools)

    def test_a_host_pin_the_image_holds_is_refused(self):
        with self.assertRaisesRegex(ValueError, "router: .* is in this image's store, but the "
                                                "build host found it in no closure list"):
            ti.image_table(self.facts, self.tools)

    def test_the_tools_outside_nix_are_exactly_the_images(self):
        self.in_the_image()
        for tools in (self.tools[1:], self.tools + self.tools[:1],
                      self.tools + [f"extra=1:{self.base}"]):
            with self.subTest(tools=tools):
                with self.assertRaisesRegex(ValueError, "an image names each of adk-drc, "
                                                        "adk-verify-carrier-drc, "
                                                        "assembly-lvs, chiplet-cosim, "
                                                        "chiplet-studio, git, intm4tm2-run-drc, "
                                                        "kicad-cli, klayout, klayout-python, "
                                                        "python once"):
                    ti.image_table(self.facts, tools)

    def test_a_tracked_tool_is_one_of_the_images(self):
        self.in_the_image()
        for name in ("assembly-lvs", "chiplet-cosim"):
            with self.subTest(name=name):
                repo = clean_repo(self.base / f"{name}-checkout")
                spec = f"{name}={self.base / 'opt' / name / name}:{repo}"
                tools = [t for t in self.tools if not t.startswith(f"{name}=")]
                data, _ = ti.image_table(self.facts, tools, [spec])
                row = {t["name"]: t for t in json.loads(data)["tools"]}[name]
                self.assertEqual((row["from"], row["path"], row["version"]),
                                 ("image", None, ti.tracked_version(repo)))
                with self.assertRaisesRegex(ValueError, "an image names each of"):
                    ti.image_table(self.facts, self.tools, [spec])

    def test_a_tool_spec_is_refused_unless_its_paths_are_absolute(self):
        for spec in ("python=Python 3:usr/bin/python3", "python=Python 3:/usr/bin/python3:tree",
                     "python=a:b:/usr/bin/python3", "=1:/x", "python=:/x"):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(ValueError, "--tool wants NAME=VERSION:BINARY"):
                    ti.tool_spec(spec)

    def test_an_image_pin_needing_a_path_no_list_names_is_refused(self):
        self.closures[self.paths["engine"]].append(self.paths["router"])
        self.answer()
        with self.assertRaisesRegex(ValueError, "my-engine: .* is in the image's store, but it "
                                                "needs .*-router, which no closure list names"):
            ti.predict(self.pin, ti.IMAGE_DROP)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(HERE / "tool_identity.py"), *args],
                              capture_output=True, text=True, check=False)

    def test_the_facts_cli(self):
        out = self.base / "facts-cli.json"
        made = self.run_cli("facts", "--pin", str(self.pin), "--out", str(out))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(sorted(json.loads(out.read_text())["pins"]), ["my-engine", "router"])
        out.unlink()
        self.pin.write_text(self.lane_pin)
        refused = self.run_cli("facts", "--pin", str(self.pin), "--out", str(out))
        self.assertEqual((refused.returncode, refused.stdout, out.exists()), (1, "", False))
        self.assertEqual(refused.stderr, "tool_identity: the pins named like an image tool are []; "
                                         "the image leaves out exactly ['python']\n")

    def test_the_cli(self):
        facts = self.base / "facts.json"
        facts.write_bytes(ti.canonical({"pins": self.facts}))
        self.in_the_image()
        table = self.base / "tool-identity.json"
        tools = [a for t in self.tools for a in ("--tool", t)]
        made = self.run_cli("image", "--facts", str(facts), *tools, "--out", str(table))
        self.assertEqual(made.returncode, 0, made.stderr)
        digest = hashlib.sha256(table.read_bytes()).hexdigest()
        self.assertEqual(made.stdout.splitlines(), [
            "2 paths of this image's store serialised, each the build host's narHash",
            f"{digest}  {table}"])
        checked = self.run_cli("verify", "--facts", str(facts), *tools, "--table", str(table))
        self.assertEqual((checked.returncode, checked.stderr), (0, ""))
        self.assertEqual(checked.stdout,
                         f"{digest}  {table}: the table this image gives, 13 tools; 2 paths of "
                         f"its store serialised, each the build host's narHash\n")
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            pass
        else:
            schema = self.run_cli("verify", "--facts", str(facts), *tools, "--table", str(table),
                                  "--schema", str(SCHEMA))
            self.assertEqual(schema.returncode, 0, schema.stderr)
            self.assertTrue(schema.stdout.endswith("; valid against its schema\n"))
        good = table.read_bytes()
        record = json.loads(good)
        next(t for t in record["tools"] if t["name"] == "assembly-lvs")["binary_sha256"] = "0" * 64
        for bytes_, want in (
                (ti.canonical(record), f"assembly-lvs: binary_sha256 '{'0' * 64}', rebuilt "),
                (json.dumps(json.loads(good), indent=1).encode(),
                 "the same entries in other bytes"),
                (b'{"tools":[]}', "format: None, rebuilt "),
                (b"not json", "it is not a table")):
            with self.subTest(want=want):
                table.write_bytes(bytes_)
                bad = self.run_cli("verify", "--facts", str(facts), *tools, "--table", str(table))
                self.assertEqual((bad.returncode, bad.stdout), (1, ""))
                self.assertIn(f"tool_identity: {table} is not the table this image gives: {want}",
                              bad.stderr)
        # A store path that moved since the table was written fails the rebuild.
        table.write_bytes(good)
        (Path(self.paths["engine"]) / "bin" / "engine").write_text("moved\n")
        moved = self.run_cli("verify", "--facts", str(facts), *tools, "--table", str(table))
        self.assertEqual(moved.returncode, 1)
        self.assertIn("serialises to NAR sha256", moved.stderr)
        facts.write_text('{"pins": {"x": {"from": "elsewhere"}}}')
        odd = self.run_cli("image", "--facts", str(facts), *tools, "--out", str(table))
        self.assertEqual(odd.returncode, 1)
        self.assertIn("x is not a pin as `facts` writes one", odd.stderr)


class ImageScript(unittest.TestCase):
    """The specs nix/tool-identity-image.sh hands the image's table, read from the script."""

    def test_the_python_row_names_the_venv(self):
        # The venv's python3 is the system interpreter by realpath, the same
        # binary system Python has, so only the tree tells the two apart.
        text = (HERE / "tool-identity-image.sh").read_text()
        adk = re.search(r"^ADK=(\S+)$", text, re.MULTILINE).group(1)
        py = re.search(r'^PY="(\S+)"$', text, re.MULTILINE).group(1).replace("$ADK", adk)
        specs = re.findall(r'^\s*--tool "python=(.*)" \\$', text, re.MULTILINE)
        self.assertEqual(len(specs), 1, specs)
        spec = (specs[0].replace('$("$PY" -V)', "Python 3")
                .replace("$PY", py).replace("$ADK", adk))
        _, _, binary, tree = ti.tool_spec("python=" + spec)
        self.assertEqual((binary, tree), (f"{adk}/venv/bin/python3", f"{adk}/venv"))


if __name__ == "__main__":
    unittest.main()
