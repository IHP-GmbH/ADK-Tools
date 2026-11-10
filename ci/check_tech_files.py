#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Every layer sheet and stackup packaging-flow declares is in the image and readable.

A system description Technology may name a KLayout layer sheet (layer_properties)
and a stackup YAML. packaging-flow writes them as ${VAR} paths, a System's
.chiplet carries them as written, and Chiplet Studio expands each ${VAR} from
the environment when it opens the assembly. A path that does not resolve fails
that load, and a sheet that resolves to the wrong file colours a die's layers
wrongly with nothing said, so the image holds the files packaging-flow names,
under the variables it names.

The strings are read from packaging-flow itself, never from a list kept here:
packaging_flow.technologies, imported from the staged tree's src/, declares
every technology a spec may build from in its module-level TECHNOLOGIES, and
that tuple is read. The module must be the staged tree's own: one imported
from anywhere else, an installed copy say, is refused.

Checked, every failure reported:

  1. packaging_flow.technologies imports from the staged tree and defines
     TECHNOLOGIES, a non-empty tuple whose items carry an id,
     layer_properties and stackup;
  2. every path is anchored: it starts with ${VAR} or is absolute, as the
     model requires; each ${VAR} in it is a name Studio's path table knows,
     set in this environment to a directory that holds the table's marker
     for it; and the expanded path is a regular file;
  3. a sheet parses as XML and names at least one layer by number (a <source>
     of the form layer/datatype); a stackup holds at least one layer, a
     top-level name with an index and a type, which is what Studio reads;
  4. a sheet has its technology file beside it, the same name with .lyt:
     packaging_flow.klayout_technology opens a layout with -nn on that file,
     and without it KLayout opens the layout under no technology.

Reported, not failed: a stackup layer that its technology's sheet does not
name. Studio still draws it, uncoloured by the sheet.

Studio's table (kPathVarMarkers in src/formats/ChipletFormat.cpp) is the
whole vocabulary: it refuses a name outside it even when a variable of that
name is set, and it takes an environment value only when the table's marker
subpath is a directory under it, otherwise it walks up from the .chiplet. The
table is read from the Studio source the image was built from, never copied
here. A walk is not imitated: the image sets every root, and a root without
its marker fails here rather than being left to a walk.

Usage:
    check_tech_files.py [PACKAGING_FLOW_ROOT] [--studio-source CHIPLETFORMAT_CPP]
    check_tech_files.py --self-test

The defaults are the image's paths, so the verify stage and
ci/check_image_tech_files.sh run the same invocation.
"""

import argparse
import importlib
import os
import pathlib
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

MODULE = "packaging_flow.technologies"
ATTR = "TECHNOLOGIES"
FIELDS = (("layer_properties", "sheet"), ("stackup", "stackup"))
VAR = re.compile(r"\$\{([^}]*)\}")
LAYER_SOURCE = re.compile(r"(\d+)/(\d+)")
# Studio's path table: the kPathVarMarkers array, one entry per name,
# { "NAME", { "dir", ... }, "marker/subpath" }. The walk list may be
# { nullptr } and the marker "" (an env-only root, taken when it is a directory).
STUDIO_TABLE = re.compile(r"kPathVarMarkers\s*\[\s*\]\s*=\s*\{(.*?)\n\};", re.S)
STUDIO_ENTRY = re.compile(
    r'\{\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,\s*\{[^{}]*\}\s*,\s*"([^"]*)"\s*\}')
# A stackup layer as Studio reads one: a top-level key, then its index and type.
STACKUP_KEY = re.compile(r"^([^\s#][^:]*):\s*(?:#.*)?$")
STACKUP_FIELD = re.compile(r"^\s+(index|type):\s*(\d+)\s*(?:#.*)?$")


def import_technologies(root):
    """MODULE imported fresh with root/src first on sys.path; raises if it is not root's."""
    src = os.path.realpath(os.path.join(root, "src"))

    def forget():
        for name in [n for n in sys.modules if n.split(".")[0] == "packaging_flow"]:
            del sys.modules[name]

    forget()
    sys.path.insert(0, src)
    try:
        module = importlib.import_module(MODULE)
    finally:
        sys.path.remove(src)
        forget()
    where = os.path.realpath(getattr(module, "__file__", None) or "")
    if not where.startswith(src + os.sep):
        raise ImportError("%s was imported from %s, not from %s" % (MODULE, where, src))
    return module


def declared(root, importer=import_technologies):
    """(technologies, [failures])."""
    try:
        module = importer(root)
    except Exception as exc:  # noqa: BLE001 - any import failure is reported
        return (), ["%s does not import (%s: %s)" % (MODULE, type(exc).__name__, exc)]
    techs = getattr(module, ATTR, None)
    if not isinstance(techs, tuple) or not techs:
        return (), ["%s defines no non-empty %s tuple" % (MODULE, ATTR)]
    bad = [t for t in techs
           if not all(hasattr(t, a) for a in ("id", "layer_properties", "stackup"))]
    if bad:
        return (), ["%s.%s holds %d item(s) that are not technologies"
                    % (MODULE, ATTR, len(bad))]
    return techs, []


def studio_table(text):
    """{name: marker subpath} from ChipletFormat.cpp's text, or raises ValueError."""
    block = STUDIO_TABLE.search(text)
    if not block:
        raise ValueError("holds no kPathVarMarkers[] = { ... }; array")
    body = re.sub(r"//[^\n]*", "", block.group(1))
    # Every top-level { ... } is one entry, and each must read whole: an entry
    # in a shape this check does not know is refused, never skipped.
    entries, depth, start, quoted = [], 0, None, False
    for i, ch in enumerate(body):
        if ch == '"' and body[i - 1] != "\\":
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                entries.append(body[start:i + 1])
    if not entries:
        raise ValueError("kPathVarMarkers holds no entry")
    table = {}
    for entry in entries:
        m = STUDIO_ENTRY.fullmatch(entry)
        if not m:
            raise ValueError("kPathVarMarkers holds an entry this check cannot read: %s"
                             % " ".join(entry.split()))
        table[m.group(1)] = m.group(2)
    return table


def expand(value, environ, table):
    """(expanded path, None) or (None, why)."""
    if not (value.startswith("${") or os.path.isabs(value)):
        return None, "is neither ${VAR}-anchored nor absolute"
    for name in VAR.findall(value):
        if name not in table:
            return None, ("names ${%s}, which Studio's path table does not know "
                          "(it knows %s)" % (name, ", ".join(sorted(table))))
        root = environ.get(name)
        if not root:
            return None, "names ${%s}, not set in this environment" % name
        if not os.path.isdir(os.path.join(root, table[name])):
            return None, ("names ${%s}=%s, which %s, so Studio would not take it"
                          % (name, root, "holds no %s" % table[name] if table[name]
                             else "is not a directory"))
    out = VAR.sub(lambda m: environ[m.group(1)], value)
    if not os.path.isabs(out):
        return None, "expands to the relative path %s" % out
    return out, None


def reads(techs, environ, table):
    """{path: "file" or "dir"}: what check() reads for these technologies, each
    file a field names, the .lyt beside a sheet, and the marker directory of
    each root a field names. A field whose roots are not all set names nothing."""
    out = {}
    for tech in techs:
        for field, kind in FIELDS:
            value = getattr(tech, field)
            if value is None:
                continue
            names = VAR.findall(value)
            for name in names:
                if name in table and environ.get(name):
                    out[os.path.join(environ[name], table[name]).rstrip("/")] = "dir"
            if all(environ.get(n) for n in names):
                path = VAR.sub(lambda m: environ[m.group(1)], value)
                out[path] = "file"
                if kind == "sheet":
                    out[technology_file(path)] = "file"
    return out


def technology_file(sheet):
    """The .lyt beside a sheet: packaging_flow.klayout_technology.files
    opens a layout with -nn on lyp.with_suffix(".lyt")."""
    return os.path.splitext(sheet)[0] + ".lyt"


def sheet_layers(path):
    """{(layer, datatype)}, or raises ValueError."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError("is not XML (%s)" % exc)
    layers = set()
    for source in root.iter("source"):
        m = LAYER_SOURCE.search(source.text or "")
        if m:
            layers.add((int(m.group(1)), int(m.group(2))))
    if not layers:
        raise ValueError("names no layer by number")
    return layers


def stackup_layers(path):
    """{(layer, datatype): name}, or raises ValueError."""
    layers, name, fields = {}, None, {}

    def close():
        if name is not None and "index" in fields and "type" in fields:
            layers[(fields["index"], fields["type"])] = name

    with open(path, encoding="utf-8") as f:
        for line in f:
            key = STACKUP_KEY.match(line)
            if key:
                close()
                name, fields = key.group(1).strip(), {}
                continue
            field = STACKUP_FIELD.match(line)
            if field and name is not None:
                fields[field.group(1)] = int(field.group(2))
    close()
    if not layers:
        raise ValueError("holds no layer with an index and a type")
    return layers


def check(root, environ, table, importer=import_technologies):
    """(report lines, notes, failures). It reads what reads() names; keep the two together."""
    techs, failures = declared(root, importer)
    named = sorted({(tech.id, kind, getattr(tech, field))
                    for tech in techs for field, kind in FIELDS
                    if getattr(tech, field) is not None})
    lines, notes, sheets, stackups = [], [], {}, {}
    for tech, kind, value in named:
        where = "%s %s %s" % (tech, kind, value)
        path, why = expand(value, environ, table)
        if why is None and not os.path.isfile(path):
            why = "is not a file here (%s)" % path
        if why is None:
            try:
                if kind == "sheet":
                    sheets.setdefault(tech, {})[value] = sheet_layers(path)
                    count = len(sheets[tech][value])
                    if not os.path.isfile(technology_file(path)):
                        raise ValueError("has no technology file %s beside it"
                                         % technology_file(path))
                else:
                    stackups.setdefault(tech, {})[value] = stackup_layers(path)
                    count = len(stackups[tech][value])
            except (OSError, ValueError) as exc:
                why = "%s %s" % (path, exc)
        if why is not None:
            failures.append("%s %s" % (where, why))
            continue
        lines.append("%s: %d layers" % (where, count))
    for tech in sorted(stackups):
        known = set().union(*sheets.get(tech, {}).values()) if tech in sheets else None
        if known is None:
            continue
        for value, layers in sorted(stackups[tech].items()):
            for pair, name in sorted(layers.items()):
                if pair not in known:
                    notes.append("not failed: %s stackup layer %d/%d (%s) is not in its sheet"
                                 % (tech, pair[0], pair[1], name))
    return lines, notes, failures


class Tech:
    def __init__(self, id, layer_properties=None, stackup=None):
        self.id, self.layer_properties, self.stackup = id, layer_properties, stackup


SHEET = """<?xml version="1.0" encoding="utf-8"?>
<layer-properties>
 <properties><name>M1</name><source>8/0@1</source></properties>
 <properties><name>V1</name><source>Via1 19/0@1</source></properties>
 <properties><name>marker</name><source>*/*@*</source></properties>
</layer-properties>
"""
STACKUP = """# a stackup
Metal1:
  index: 8
  type: 0
  z: 0.5
Via1:
  index: 19   # cut
  type: 0
Substrate:
  index: 40
  type: 0
"""


def self_test():
    cases = []

    def case(name):
        def register(fn):
            cases.append((name, fn))
            return fn
        return register

    def tree(tmp, module=None):
        root = pathlib.Path(tmp, "pf")
        (root / "pdk").mkdir(parents=True)
        (root / "pdk" / "t.lyp").write_text(SHEET)
        (root / "pdk" / "t.lyt").write_text("<technology/>\n")
        (root / "pdk" / "t.yaml").write_text(STACKUP)
        if module is not None:
            pkg = root / "src" / "packaging_flow"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "technologies.py").write_text(module)
        return root

    def fake(value):
        def importer(root):
            if isinstance(value, Exception):
                raise value
            return type("M", (), {} if value is None else {ATTR: value})
        return importer

    def run(value, environ):
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp)
            env = {k: v.replace("<root>", str(root)) for k, v in environ.items()}
            return check(root, env, TABLE, fake(value))

    GOOD = (Tech("t", "${PDK}/pdk/t.lyp", "${PDK}/pdk/t.yaml"), Tech("u"))
    ENV = {"PDK": "<root>"}
    TABLE = {"PDK": "pdk", "OTHER": "x", "BARE": ""}
    MODULE_TEXT = (
        "from types import SimpleNamespace as T\n"
        "TECHNOLOGIES = (T(id='t', layer_properties='${PDK}/pdk/t.lyp', stackup=None),)\n")

    @case("the technologies pass, one line per file, Substrate noted")
    def _():
        lines, notes, failures = run(GOOD, ENV)
        return (not failures and len(lines) == 2
                and lines[0] == "t sheet ${PDK}/pdk/t.lyp: 2 layers"
                and lines[1] == "t stackup ${PDK}/pdk/t.yaml: 3 layers"
                and notes == ["not failed: t stackup layer 40/0 (Substrate) is not in its sheet"])

    @case("reads() names every path check() looks at, and no other")
    def _():
        seen, real = set(), (os.path.isfile, os.path.isdir, open)
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp)
            env = {"PDK": str(root)}

            def note(fn):
                def wrapped(path, *args, **kwargs):
                    if str(path).startswith(str(root)):
                        seen.add(os.path.normpath(str(path)))
                    return fn(path, *args, **kwargs)
                return wrapped
            os.path.isfile, os.path.isdir = note(real[0]), note(real[1])
            globals()["open"] = note(real[2])
            try:
                _, _, failures = check(root, env, TABLE, fake(GOOD))
            finally:
                os.path.isfile, os.path.isdir = real[0], real[1]
                globals().pop("open")
            named = {os.path.normpath(p) for p in reads(GOOD, env, TABLE)}
        return not failures and seen == named

    @case("two technologies naming the same file check it once each")
    def _():
        lines, _, failures = run((Tech("t", "${PDK}/pdk/t.lyp"), Tech("v", "${PDK}/pdk/t.lyp")), ENV)
        return not failures and [l.split()[0] for l in lines] == ["t", "v"]

    @case("a module without TECHNOLOGIES fails")
    def _():
        _, _, failures = run(None, ENV)
        return failures == ["packaging_flow.technologies defines no non-empty TECHNOLOGIES tuple"]

    @case("an empty TECHNOLOGIES, or a list, fails")
    def _():
        _, _, f1 = run((), ENV)
        _, _, f2 = run(list(GOOD), ENV)
        return len(f1) == 1 and len(f2) == 1

    @case("an item that is not a technology fails")
    def _():
        _, _, failures = run(("gf180mcu",), ENV)
        return failures == [
            "packaging_flow.technologies.TECHNOLOGIES holds 1 item(s) that are not technologies"]

    @case("a module that does not import fails, naming the error")
    def _():
        _, _, failures = run(ImportError("no module x"), ENV)
        return failures == ["packaging_flow.technologies does not import (ImportError: no module x)"]

    @case("an unset variable fails, naming it")
    def _():
        _, _, failures = run(GOOD[:1], {})
        return (len(failures) == 2
                and all("names ${PDK}, not set in this environment" in f for f in failures))

    @case("a variable set to the empty string counts as unset")
    def _():
        _, _, failures = run(GOOD[:1], {"PDK": ""})
        return len(failures) == 2

    @case("a relative path fails")
    def _():
        _, _, failures = run((Tech("t", "pdk/t.lyp"),), ENV)
        return len(failures) == 1 and "neither ${VAR}-anchored nor absolute" in failures[0]

    @case("a missing file fails")
    def _():
        _, _, failures = run((Tech("t", "${PDK}/pdk/none.lyp"),), ENV)
        return len(failures) == 1 and "is not a file here" in failures[0]

    @case("a sheet that is not XML, or names no numbered layer, fails")
    def _():
        _, _, f1 = run((Tech("t", "${PDK}/pdk/t.yaml"),), ENV)
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp)
            (root / "pdk" / "t.lyp").write_text(
                "<layer-properties><properties><source>*/*@*</source>"
                "</properties></layer-properties>")
            _, _, f2 = check(root, {"PDK": str(root)}, TABLE,
                             fake((Tech("t", "${PDK}/pdk/t.lyp"),)))
        return ("is not XML" in "".join(f1)) and ("names no layer by number" in "".join(f2))

    @case("a sheet without its .lyt beside it fails; a stackup needs none")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp)
            (root / "pdk" / "t.lyt").unlink()
            lines, notes, failures = check(root, {"PDK": str(root)}, TABLE, fake(GOOD))
        return (len(failures) == 1 and lines == ["t stackup ${PDK}/pdk/t.yaml: 3 layers"]
                and notes == ["not failed: t stackup layer 40/0 (Substrate) is not in its sheet"]
                and failures[0].endswith("has no technology file %s beside it"
                                         % (root / "pdk" / "t.lyt")))

    @case("a stackup with no index/type layer fails")
    def _():
        _, _, failures = run((Tech("t", None, "${PDK}/pdk/t.lyp"),), ENV)
        return len(failures) == 1 and "holds no layer with an index and a type" in failures[0]

    @case("a stackup with no sheet for its technology is checked but not compared")
    def _():
        lines, notes, failures = run((Tech("t", None, "${PDK}/pdk/t.yaml"),), ENV)
        return not failures and len(lines) == 1 and not notes

    @case("an absolute path passes without a variable")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp)
            lines, _, failures = check(root, {}, TABLE, fake(
                (Tech("t", str(root / "pdk" / "t.lyp")),)))
        return not failures and len(lines) == 1

    @case("a name Studio's table does not know fails, even when it is set")
    def _():
        _, _, failures = run((Tech("t", "${HOME}/pdk/t.lyp"),), {"HOME": "<root>"})
        return (len(failures) == 1
                and "${HOME}, which Studio's path table does not know (it knows BARE, OTHER, PDK)"
                in failures[0])

    @case("a root without the table's marker under it fails")
    def _():
        _, _, failures = run((Tech("t", "${PDK}/t.lyp"),), {"PDK": "<root>/pdk"})
        return len(failures) == 1 and "which holds no pdk, so Studio would not take it" in failures[0]

    @case("an empty marker takes a root that is a directory and refuses one that is not")
    def _():
        lines, _, f1 = run((Tech("t", "${BARE}/t.lyp"),), {"BARE": "<root>/pdk"})
        _, _, f2 = run((Tech("t", "${BARE}/t.lyp"),), {"BARE": "<root>/pdk/t.lyp"})
        return (not f1 and len(lines) == 1 and len(f2) == 1
                and "which is not a directory, so Studio would not take it" in f2[0])

    @case("Studio's table is read from its source, comments and all, both shapes")
    def _():
        dev = (
            "struct PathVarMarker { const char* name; };\n"
            "constexpr PathVarMarker kPathVarMarkers[] = {\n"
            '    { "INTERPOSER_PDK_ROOT", { "interposer", "OpenIntM4TM2" },\n'
            '      "libs.tech/klayout" },\n'
            '    // { "COMMENTED_ROOT", { "x" }, "y" },\n'
            '    { "GDS_TO_KICAD_ROOT", { "gds_to_kicad", "gds-to-kicad" }, "pdks" },\n'
            "    // Base SG13G2 PDK (standard IHP convention: $PDK_ROOT/ihp-sg13g2/...).\n"
            '    { "PDK_ROOT", { "IHP-Open-PDK" }, "ihp-sg13g2/libs.tech/klayout" },\n'
            '    { "CHIPLET_FLOW_TECH", { nullptr }, "" },\n'
            '    { "GF180MCU_PDK_ROOT", { "gf180mcu", "wafer-space-gf180mcu" },\n'
            '      "gf180mcuD/libs.tech/klayout" },\n'
            "};\n"
            'const char* other = "{ \\"NOT_A_ROOT\\", { }, \\"z\\" }";\n')
        return studio_table(dev) == {
            "INTERPOSER_PDK_ROOT": "libs.tech/klayout", "GDS_TO_KICAD_ROOT": "pdks",
            "PDK_ROOT": "ihp-sg13g2/libs.tech/klayout", "CHIPLET_FLOW_TECH": "",
            "GF180MCU_PDK_ROOT": "gf180mcuD/libs.tech/klayout"}

    @case("a source without the table, an empty one, or an entry of another shape is refused")
    def _():
        refused = 0
        for text in ("int main() {}\n",
                     "constexpr X kPathVarMarkers[] = {\n    // none\n};\n",
                     'constexpr X kPathVarMarkers[] = {\n    { "A", { "a" }, "m" },\n'
                     '    { "B", "b", "m", 1 },\n};\n'):
            try:
                studio_table(text)
            except ValueError as exc:
                refused += 1
                last = str(exc)
        return refused == 3 and 'cannot read: { "B", "b", "m", 1 }' in last

    @case("the real importer reads the staged tree's src/packaging_flow/technologies.py")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp, MODULE_TEXT)
            lines, _, failures = check(root, {"PDK": str(root)}, TABLE)
        return not failures and len(lines) == 1 and MODULE not in sys.modules

    @case("a tree without the module fails, and an installed copy elsewhere is not taken")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            other = tree(pathlib.Path(tmp, "other"), MODULE_TEXT) / "src"
            root = tree(pathlib.Path(tmp, "staged"))
            sys.path.append(str(other))
            try:
                _, _, failures = check(root, {"PDK": str(root)}, TABLE)
            finally:
                sys.path.remove(str(other))
        return (len(failures) == 1 and "does not import (ImportError:" in failures[0]
                and "was imported from %s" % os.path.realpath(other) in failures[0])

    failed = []
    for name, fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001 - a crashing case is a failed case
            ok = False
            name = "%s (raised %s: %s)" % (name, type(exc).__name__, exc)
        if not ok:
            failed.append(name)
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default="/opt/adk-tools/staging/packaging-flow",
                    help="the staged packaging-flow tree (default: the image's)")
    ap.add_argument("--studio-source", default="/opt/adk-tools/ci/studio-ChipletFormat.cpp",
                    help="Chiplet Studio's src/formats/ChipletFormat.cpp (default: the image's)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    # Line by line, so the report and the FAIL lines keep their order in a log.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        with open(args.studio_source, encoding="utf-8") as f:
            table = studio_table(f.read())
    except (OSError, ValueError) as exc:
        print("FAIL: %s %s" % (args.studio_source, exc), file=sys.stderr)
        return 1
    print("Studio's path table: %s" % ", ".join(sorted(table)))
    lines, notes, failures = check(pathlib.Path(args.root), os.environ, table)
    for line in lines + notes:
        print(line)
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("every layer sheet, its .lyt and stackup %s names is in the image" % MODULE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
