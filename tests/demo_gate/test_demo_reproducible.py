"""The demo reproducibility gate's GDS digest, on streams written here by hand."""

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
_loader = SourceFileLoader("demo_gate",
                           str(ROOT / "bin" / "adk-verify-demo-reproducible"))
gate = module_from_spec(spec_from_loader("demo_gate", _loader))
_loader.exec_module(gate)


def rec(rectype, datatype=0, payload=b""):
    return struct.pack(">HBB", 4 + len(payload), rectype, datatype) + payload


def i16(*values):
    return struct.pack(">%dh" % len(values), *values)


def i32(*values):
    return struct.pack(">%di" % len(values), *values)


def name(text):
    raw = text.encode()
    return raw + b"\0" * (len(raw) % 2)


def boundary(layer, datatype, box):
    x0, y0, x1, y1 = box
    return (rec(0x08) + rec(0x0D, 2, i16(layer)) + rec(0x0E, 2, i16(datatype))
            + rec(0x10, 3, i32(x0, y0, x1, y0, x1, y1, x0, y1, x0, y0))
            + rec(0x11))


def sref(cell, x, y):
    return rec(0x0A) + rec(0x12, 6, name(cell)) + rec(0x10, 3, i32(x, y)) + rec(0x11)


def text(layer, label, x, y):
    return (rec(0x0C) + rec(0x0D, 2, i16(layer)) + rec(0x16, 2, i16(0))
            + rec(0x10, 3, i32(x, y)) + rec(0x19, 6, name(label)) + rec(0x11))


def stamp(t):
    return i16(*([2026, 9, 26, 12, 0, t] * 2))


def cell(cellname, elements, t=0):
    return (rec(0x05, 2, stamp(t)) + rec(0x06, 6, name(cellname))
            + b"".join(elements) + rec(0x07))


def library(cells, t=0, units=(1e-3, 1e-9), padding=b"", libname="LIB"):
    def real8(value):
        # GDSII excess-64 base-16 real, enough for the two unit values here.
        if value == 0:
            return b"\0" * 8
        exponent = 64
        while value < 1 / 16:
            value *= 16
            exponent -= 1
        while value >= 1:
            value /= 16
            exponent += 1
        return bytes([exponent]) + int(value * (1 << 56)).to_bytes(7, "big")
    return (rec(0x00, 2, i16(600)) + rec(0x01, 2, stamp(t))
            + rec(0x02, 6, name(libname)) + rec(0x03, 5, real8(units[0]) + real8(units[1]))
            + b"".join(cells) + rec(0x04) + padding)


A = boundary(134, 0, (0, 0, 100, 100))
B = boundary(134, 0, (200, 0, 300, 100))
C = boundary(67, 0, (0, 0, 50, 50))
T = text(134, "VDD", 10, 10)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="demo gate ")
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def write(self, data):
        self.count += 1
        path = Path(self.tmp.name) / ("s%d.gds" % self.count)
        path.write_bytes(data)
        return path

    def digest(self, data):
        return gate._gds_geometry_digest(self.write(data))

    def base(self, t=0, **kw):
        return library([cell("TOP", [A, B, C, T, sref("HEAL", 0, 0)], t=t),
                        cell("HEAL", [A, C], t=t)], t=t, **kw)

    def test_timestamps_are_ignored(self):
        self.assertEqual(self.digest(self.base(t=1)), self.digest(self.base(t=59)))

    def test_cell_timestamps_alone_are_ignored(self):
        one = library([cell("TOP", [A, B], t=1), cell("HEAL", [C], t=2)])
        two = library([cell("TOP", [A, B], t=40), cell("HEAL", [C], t=50)])
        self.assertEqual(self.digest(one), self.digest(two))

    def test_element_order_is_ignored(self):
        other = library([cell("TOP", [sref("HEAL", 0, 0), T, C, B, A]),
                         cell("HEAL", [C, A])])
        self.assertEqual(self.digest(self.base()), self.digest(other))

    def test_cell_order_is_ignored(self):
        other = library([cell("HEAL", [A, C]),
                         cell("TOP", [A, B, C, T, sref("HEAL", 0, 0)])])
        self.assertEqual(self.digest(self.base()), self.digest(other))

    def test_padding_after_endlib_is_ignored(self):
        self.assertEqual(self.digest(self.base()),
                         self.digest(self.base(padding=b"\0" * 6)))

    def test_a_real_change_flips_it(self):
        base = self.digest(self.base())
        changes = {
            "coordinate": library([cell("TOP", [A, boundary(134, 0, (200, 0, 305, 100)),
                                                C, T, sref("HEAL", 0, 0)]),
                                   cell("HEAL", [A, C])]),
            "layer": library([cell("TOP", [A, boundary(126, 0, (200, 0, 300, 100)),
                                           C, T, sref("HEAL", 0, 0)]),
                              cell("HEAL", [A, C])]),
            "datatype": library([cell("TOP", [A, boundary(134, 1, (200, 0, 300, 100)),
                                              C, T, sref("HEAL", 0, 0)]),
                                 cell("HEAL", [A, C])]),
            "element removed": library([cell("TOP", [A, C, T, sref("HEAL", 0, 0)]),
                                        cell("HEAL", [A, C])]),
            "duplicate added": library([cell("TOP", [A, A, B, C, T, sref("HEAL", 0, 0)]),
                                        cell("HEAL", [A, C])]),
            "moved between cells": library([cell("TOP", [A, B, T, sref("HEAL", 0, 0)]),
                                            cell("HEAL", [A, C, C])]),
            "label text": library([cell("TOP", [A, B, C, text(134, "VSS", 10, 10),
                                                sref("HEAL", 0, 0)]),
                                   cell("HEAL", [A, C])]),
            "instance offset": library([cell("TOP", [A, B, C, T, sref("HEAL", 5, 0)]),
                                        cell("HEAL", [A, C])]),
            "cell renamed": library([cell("TOP", [A, B, C, T, sref("HEAL", 0, 0)]),
                                     cell("HEAL2", [A, C])]),
            "cell added": library([cell("TOP", [A, B, C, T, sref("HEAL", 0, 0)]),
                                   cell("HEAL", [A, C]), cell("EMPTY", [])]),
            "units": self.base(units=(1e-3, 1e-8)),
            "library name": self.base(libname="LIB2"),
        }
        for label, data in changes.items():
            with self.subTest(change=label):
                self.assertNotEqual(base, self.digest(data))

    def test_records_stay_bound_to_their_element(self):
        # Same records per cell, differently assembled into elements: a metal
        # shape and a via trading layers must not pass as unchanged.
        one = library([cell("TOP", [boundary(134, 0, (0, 0, 10, 10)),
                                    boundary(67, 0, (20, 0, 30, 10))])])
        two = library([cell("TOP", [boundary(67, 0, (0, 0, 10, 10)),
                                    boundary(134, 0, (20, 0, 30, 10))])])
        self.assertNotEqual(self.digest(one), self.digest(two))

    def test_record_order_inside_an_element_is_kept(self):
        # Property values traded between two attributes of one shape: the
        # same records in another order, and not the same shape.
        def tagged(first, second):
            return (A[:-4] + rec(0x2B, 2, i16(1)) + rec(0x2C, 6, name(first))
                    + rec(0x2B, 2, i16(2)) + rec(0x2C, 6, name(second)) + rec(0x11))
        self.assertNotEqual(self.digest(library([cell("TOP", [tagged("a", "b")])])),
                            self.digest(library([cell("TOP", [tagged("b", "a")])])))

    def test_multiplicity_counts_at_the_same_element_count(self):
        self.assertNotEqual(self.digest(library([cell("TOP", [A, A, B])])),
                            self.digest(library([cell("TOP", [A, B, B])])))

    def test_same_elements_in_other_cells_are_not_confused(self):
        one = library([cell("X", [A, B]), cell("Y", [])])
        two = library([cell("X", [A]), cell("Y", [B])])
        self.assertNotEqual(self.digest(one), self.digest(two))

    def test_malformed_streams_are_refused(self):
        whole = self.base()
        cases = {
            "truncated": whole[:-10],
            "no ENDLIB": whole[:-4],
            "element outside a cell": library([A]),
            "element never ends": library([cell("TOP", [A[:-4]])]),
            "cell inside a cell": library([rec(0x05, 2, stamp(0)) + rec(0x06, 6, name("OUT"))
                                           + cell("IN", [A]) + rec(0x07)]),
            "cell never ends": library([(rec(0x05, 2, stamp(0)) + rec(0x06, 6, name("TOP"))
                                         + A)]),
            "stray ENDEL in a cell": library([cell("TOP", [A, rec(0x11)])]),
            "stray ENDSTR outside a cell": library([cell("TOP", [A]), rec(0x07)]),
            "stray ENDSTR before any cell": library([rec(0x07), cell("TOP", [A])]),
            "stray ENDEL before any element": library([cell("TOP", [rec(0x11), A])]),
            "record cut short": whole[:-4] + b"\x00\x08\x04\x00",
            "cell name after an element": library([rec(0x05, 2, stamp(0)) + A
                                                   + rec(0x06, 6, name("TOP")) + rec(0x07)]),
            "library record after the cells": (whole[:-4] + rec(0x02, 6, name("LIB"))
                                               + rec(0x04)),
            "record length under 4": whole[:6] + b"\x00\x02\x00\x00" + whole[6:],
            "ENDSTR with a datatype": library([rec(0x05, 2, stamp(0)) + rec(0x06, 6, name("TOP"))
                                               + A + rec(0x07, 3)]),
            "ENDSTR with data": library([rec(0x05, 2, stamp(0)) + rec(0x06, 6, name("TOP"))
                                         + A + rec(0x07, 0, b"\x00\x00")]),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                self.assertIsNone(self.digest(data))


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="demo gate ")
        self.addCleanup(self.tmp.cleanup)
        self.tracked = Path(self.tmp.name) / "tracked"
        self.fresh = Path(self.tmp.name) / "fresh"

    def put(self, root, data):
        path = root / "layout" / "demo_interposer.gds"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_a_reordered_regen_is_reproducible(self):
        self.put(self.tracked, library([cell("TOP", [A, B, C])], t=1))
        self.put(self.fresh, library([cell("TOP", [C, A, B])], t=2))
        self.assertEqual(gate.compare(self.tracked, self.fresh), [])

    def test_a_changed_regen_is_reported(self):
        self.put(self.tracked, library([cell("TOP", [A, B, C])]))
        self.put(self.fresh, library([cell("TOP", [A, B])]))
        diffs = gate.compare(self.tracked, self.fresh)
        self.assertEqual(len(diffs), 1)
        self.assertIn("GDS geometry differs", diffs[0])

    def test_an_unparseable_pair_falls_back_to_bytes(self):
        broken = library([cell("TOP", [A])])[:-4]
        self.put(self.tracked, broken)
        self.put(self.fresh, broken)
        self.assertEqual(gate.compare(self.tracked, self.fresh), [])
        self.put(self.fresh, broken[:-2] + b"\x00\x01")
        diffs = gate.compare(self.tracked, self.fresh)
        self.assertEqual(len(diffs), 1)
        self.assertIn("unparseable stream", diffs[0])

    def test_a_regen_whose_endstr_klayout_would_refuse_is_reported(self):
        good = library([cell("TOP", [A, B])], t=1)
        bad = library([rec(0x05, 2, stamp(0)) + rec(0x06, 6, name("TOP"))
                       + A + B + rec(0x07, 3)], t=2)
        self.put(self.tracked, good)
        self.put(self.fresh, bad)
        diffs = gate.compare(self.tracked, self.fresh)
        self.assertEqual(len(diffs), 1)
        self.assertIn("unparseable stream", diffs[0])


if __name__ == "__main__":
    unittest.main()
