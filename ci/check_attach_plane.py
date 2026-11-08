#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Every IntM4TM2 .chiplet the image carries mounts its dies on the PDK's plane.

The interposer PDK states the plane dies mount on once, as
beol.attachment_surface_z_um in libs.tech/intm4tm2_stack.json (the key). The
coordinate frame contract (section 3.5) holds every committed .chiplet on an
IntM4TM2 interposer to it, and puts the check here because the image is the
only place where the PDK sits beside every consumer. A file is on IntM4TM2 when
its interposer component declares `technology: intm4tm2` or its `interposer:`
block names `adapter: intm4tm2`. For such a file:

  - the interposer's attachment_surface_z, when the file declares one, equals
    the key;
  - every component with a `connection:` and a non-zero position.z sits at the
    plane plus that connection's total height, the sum of its layers' heights,
    written as contract section 1.5 states: the exact decimal sum, as the
    shortest text that reads back as it (`N.0` is accepted for a whole N). The
    plane is the file's own when it declares one, else the key: the technology
    stackup's plane applies then, and the check below holds Studio's stackup
    to the key. A connection is the file's connection_stacks entry, else the
    interconnect PDK's method of that id; one defined in neither fails.

A failure names the file and the line. A file with more than one interposer
component, a value that is not a number, or a key that appears twice in a
mapping the check reads fails too, and so does a file that does not parse as
YAML but names intm4tm2.

Coverage. The check walks exactly three trees, each by a label: the plugin
(tools/chiplet_kicad_plugin), Studio (tools/chiplet-studio), both as their
gitlinks pin them, and adk-tools' own examples (examples). It reads every
regular file named *.chiplet under them, except under .git, Studio's in-tree
build products (build/ and extern/ at its root; the Studio build copies its
test fixtures into build/) and any conformance/fixtures directory: conformance
fixtures are format data, not designs, and contract section 3.5 keeps them out.
A symlink named *.chiplet fails. Each tree must hold at least one IntM4TM2
file, so a tree given at the wrong directory cannot pass by finding nothing.
The .chiplet files in other repositories (interposer-pnr, packaging-flow and
the rest) are not walked; their owners' refresh rules cover them.

Exemptions. A deliberately synthetic file that may keep another plane is
declared in the exemption file, one line each: its path as `<label>/<path in
the tree>`, then the reason. Its failures are reported as exempt and do not
fail the check. An exemption fails as stale when its file no longer exists,
is not an IntM4TM2 file, or now passes; a line without a reason, a duplicate,
or a path outside the three trees fails.

Copies of the PDK's files in the consumers, compared with the PDK given:

  - the plugin's test descriptor
    plugins/chiplet_export/tests/fixtures/intm4tm2_pdk/libs.tech/intm4tm2_stack.json
    is the key file, byte for byte;
  - Studio's fixture tests/fixtures/intm4tm2.stackup.yaml is the PDK's
    libs.tech/chiplet_studio/intm4tm2.stackup.yaml, byte for byte (Studio's
    own suite compares its bundled stackup with that fixture, so the fixture
    must be the PDK's file);
  - Studio's bundled configs/stackups/intm4tm2.yaml agrees with the PDK's
    stackup on every layer the PDK has, matched by GDS layer and datatype:
    name (X.filler is XFiller in Studio, X.pillar is X_pillar), purpose, z and
    height; a layer only Studio has is marked render_only; its
    attachment_surface_z equals the key.

Further copies of the key file, outside the three trees, are named with
--pdk-copy LABEL=PATH, once each: every one must be the key file byte for byte,
and one that is missing or not a regular file fails, naming its label and path.
Only the copies named are compared. The image build names the copies in the
staged repositories' test fixtures; a run without those trees names none.

Numbers compare as decimals read from their text, never as binary floats.

Usage:
    check_attach_plane.py --pdk tools/OpenIntM4TM2 \\
        --interconnect tools/IHP-Interconnect-IntM4TM2 \\
        --tree tools/chiplet_kicad_plugin=tools/chiplet_kicad_plugin \\
        --tree tools/chiplet-studio=tools/chiplet-studio \\
        --tree examples=examples \\
        --exemptions ci/attach-plane-exemptions.txt \\
        [--pdk-copy LABEL=PATH ...]
    check_attach_plane.py --self-test
"""

import argparse
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from decimal import Decimal, InvalidOperation

import yaml

PLUGIN = "tools/chiplet_kicad_plugin"
STUDIO = "tools/chiplet-studio"
EXAMPLES = "examples"
TREES = (PLUGIN, STUDIO, EXAMPLES)
# Directories skipped, relative to a tree's root, besides .git and any
# conformance/fixtures directory.
PRUNE = {STUDIO: ("build", "extern")}
TECH = "intm4tm2"

KEY = "libs.tech/intm4tm2_stack.json"
PDK_STACKUP = "libs.tech/chiplet_studio/intm4tm2.stackup.yaml"
METHODS = "manifest/interconnect_methods.json"
PLUGIN_DESCRIPTOR = "plugins/chiplet_export/tests/fixtures/intm4tm2_pdk/" + KEY
STUDIO_FIXTURE = "tests/fixtures/intm4tm2.stackup.yaml"
STUDIO_BUNDLED = "configs/stackups/intm4tm2.yaml"
STUDIO_NAMES = ((".filler", "Filler"), (".pillar", "_pillar"))


class Problem(Exception):
    pass


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def number(text):
    """A finite decimal from a number's text, or None."""
    try:
        d = Decimal(str(text).replace("_", ""))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def shortest(d):
    """The shortest text that reads back as d."""
    return format(d.normalize(), "f")


def written_as_sum(text, total):
    """True when text is the exact sum written as section 1.5 states."""
    want = shortest(total)
    return text == want or ("." not in want and text == want + ".0")


def read_key(pdk):
    path = pathlib.Path(pdk) / KEY
    try:
        data = json.loads(path.read_text(), parse_float=Decimal)
        value = data["beol"]["attachment_surface_z_um"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Problem("%s: no beol.attachment_surface_z_um (%s)" % (path, e))
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise Problem("%s: beol.attachment_surface_z_um is not a number: %r" % (path, value))
    return Decimal(value)


def read_methods(interconnect):
    """Connection id -> [(layer name, height)] from the interconnect PDK."""
    path = pathlib.Path(interconnect) / METHODS
    try:
        methods = json.loads(path.read_text(), parse_float=Decimal)["methods"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Problem("%s: no methods (%s)" % (path, e))
    out = {}
    for mid, m in methods.items():
        layers = (m.get("connection_stack") or {}).get("layers")
        if not isinstance(layers, list):
            continue
        heights = []
        for layer in layers:
            h = layer.get("height") if isinstance(layer, dict) else None
            heights.append(None if isinstance(h, bool) or not isinstance(h, (int, Decimal))
                           else Decimal(h))
        out[mid] = heights
    return out


# --- .chiplet files ---------------------------------------------------------

class Reader:
    """Lookups on composed YAML nodes that record what is wrong, with lines."""

    def __init__(self, shown):
        self.shown = shown
        self.failures = []

    def fail(self, node, message):
        self.failures.append("%s:%d: %s" % (self.shown, node.start_mark.line + 1, message))

    def get(self, node, key):
        if not isinstance(node, yaml.MappingNode):
            return None
        found = [v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == key]
        for extra in found[1:]:
            self.fail(extra, "%s appears more than once in this mapping" % key)
        return found[0] if found else None

    def text(self, node, key):
        v = self.get(node, key)
        return v.value if isinstance(v, yaml.ScalarNode) else None

    def num(self, node, what):
        if not isinstance(node, yaml.ScalarNode) or node.tag not in (
                "tag:yaml.org,2002:float", "tag:yaml.org,2002:int"):
            self.fail(node, "%s is not a number" % what)
            return None
        d = number(node.value)
        if d is None:
            self.fail(node, "%s is not a finite number: %s" % (what, node.value))
        return d


def on_intm4tm2(r, root):
    comps = r.get(root, "components")
    comps = comps.value if isinstance(comps, yaml.SequenceNode) else []
    interposers = [c for c in comps if r.text(c, "type") == "interposer"]
    adapter = r.text(r.get(root, "interposer"), "adapter")
    ours = adapter == TECH or any(r.text(c, "technology") == TECH for c in interposers)
    return ours, comps, interposers


def file_stacks(r, root):
    """Connection id -> (node, [height or None]) from the file itself."""
    out = {}
    stacks = r.get(root, "connection_stacks")
    if not isinstance(stacks, yaml.MappingNode):
        return out
    for k, v in stacks.value:
        layers = r.get(v, "layers")
        heights = []
        for layer in (layers.value if isinstance(layers, yaml.SequenceNode) else []):
            h = r.get(layer, "height")
            if h is None:
                r.fail(layer, "a layer of connection stack %s has no height" % k.value)
                heights.append(None)
            else:
                heights.append(r.num(h, "the height of a layer of %s" % k.value))
        out[k.value] = heights
    return out


def check_file(path, shown, key, methods):
    """(on intm4tm2, failures, components checked) for one .chiplet."""
    raw = pathlib.Path(path).read_bytes().decode("utf-8", "replace")
    r = Reader(shown)
    try:
        root = yaml.compose(raw, Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        if TECH in raw:
            return True, ["%s: does not parse as YAML and names %s (%s)"
                          % (shown, TECH, str(e).splitlines()[0])], 0
        return False, [], 0
    ours, comps, interposers = on_intm4tm2(r, root)
    if not ours:
        return False, [], 0
    plane = key
    if len(interposers) > 1:
        r.fail(interposers[1], "%d interposer components; the check knows the plane of one"
               % len(interposers))
    elif interposers:
        node = r.get(interposers[0], "attachment_surface_z")
        if node is not None:
            declared = r.num(node, "attachment_surface_z")
            if declared is not None and declared != key:
                r.fail(node, "attachment_surface_z is %s, the PDK's plane is %s"
                       % (node.value, shortest(key)))
            if declared is not None:
                plane = declared
    stacks = file_stacks(r, root)
    checked = 0
    for c in comps:
        if r.text(c, "type") == "interposer":
            continue
        conn = r.text(c, "connection")
        z = r.get(r.get(c, "position"), "z")
        if not conn or z is None:
            continue
        zval = r.num(z, "position.z")
        if zval is None or zval == 0:
            continue
        checked += 1
        heights = stacks.get(conn, methods.get(conn))
        if heights is None:
            r.fail(c, "connection %s is in neither the file's connection_stacks nor the "
                   "interconnect PDK's methods" % conn)
            continue
        if not heights or None in heights:
            r.fail(c, "connection %s has no complete list of layer heights" % conn)
            continue
        total = plane + sum(heights)
        if not written_as_sum(z.value, total):
            r.fail(z, "%s z is %s, the plane %s plus %s's %s is written %s"
                   % (r.text(c, "id") or "a component", z.value, shortest(plane), conn,
                      shortest(sum(heights)), shortest(total)))
    return True, r.failures, checked


def walk(label, root):
    """Sorted paths (relative to root) of every .chiplet the tree holds."""
    found, links = [], []
    prune = set(PRUNE.get(label, ()))
    for here, dirs, files in os.walk(root, followlinks=False):
        rel = os.path.relpath(here, root)
        rel = "" if rel == "." else rel + "/"
        keep = []
        for d in sorted(dirs):
            path = rel + d
            if d == ".git" or path in prune or path == "conformance/fixtures" \
                    or path.endswith("/conformance/fixtures"):
                continue
            if os.path.islink(os.path.join(here, d)):
                continue
            keep.append(d)
        dirs[:] = keep
        for f in files:
            if not f.endswith(".chiplet"):
                continue
            full = os.path.join(here, f)
            if os.path.islink(full) or not os.path.isfile(full):
                links.append(rel + f)
            else:
                found.append(rel + f)
    return sorted(found), sorted(links)


def read_exemptions(path):
    """(path -> (line number, reason), failures)."""
    out, failures = {}, []
    for n, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        where = "%s:%d" % (path, n)
        if len(parts) < 2:
            failures.append("%s: exemption for %s gives no reason" % (where, parts[0]))
            continue
        target, reason = parts
        if not any(target.startswith(t + "/") for t in TREES):
            failures.append("%s: %s is not under any of the walked trees (%s)"
                            % (where, target, ", ".join(TREES)))
            continue
        if target in out:
            failures.append("%s: %s is exempted twice" % (where, target))
            continue
        out[target] = (n, reason)
    return out, failures


def check_files(trees, key, methods, exemptions_path):
    failures, notes = [], []
    exemptions, ex_failures = read_exemptions(exemptions_path)
    failures += ex_failures
    seen = {}
    files = ours = checked = 0
    for label in TREES:
        root = trees[label]
        found, links = walk(label, root)
        for rel in links:
            failures.append("%s/%s: is a symlink or not a regular file" % (label, rel))
        tree_ours = 0
        for rel in found:
            shown = "%s/%s" % (label, rel)
            files += 1
            mine, problems, n = check_file(os.path.join(root, rel), shown, key, methods)
            seen[shown] = (mine, problems)
            if not mine:
                continue
            ours += 1
            tree_ours += 1
            checked += n
            if shown in exemptions:
                notes += ["exempt: " + p for p in problems]
            else:
                failures += problems
        if not tree_ours:
            failures.append("%s (%s): no %s .chiplet file, so the check would read nothing there"
                            % (label, root, TECH))
    for target, (n, reason) in sorted(exemptions.items()):
        where = "%s:%d" % (exemptions_path, n)
        if target not in seen:
            failures.append("%s: stale exemption, %s no longer exists" % (where, target))
        elif not seen[target][0]:
            failures.append("%s: stale exemption, %s is not an %s file" % (where, target, TECH))
        elif not seen[target][1]:
            failures.append("%s: stale exemption, %s now matches the PDK's plane; remove the line"
                            % (where, target))
    summary = ("%d .chiplet files under %d trees, %d on %s, %d components on a connection "
               "checked, %d exempt" % (files, len(TREES), ours, TECH, checked, len(exemptions)))
    return failures, notes, summary


# --- copies of the PDK's files ------------------------------------------------

def stackup_layers(path, failures):
    """(plane or None, {(index, type): (name, purpose, z, height)})."""
    r = Reader(str(path))
    try:
        root = yaml.compose(pathlib.Path(path).read_text(), Loader=yaml.SafeLoader)
    except (OSError, yaml.YAMLError) as e:
        failures.append("%s: cannot read (%s)" % (path, str(e).splitlines()[0]))
        return None, {}
    if not isinstance(root, yaml.MappingNode):
        failures.append("%s: not a mapping" % path)
        return None, {}
    plane, layers = None, {}
    names = [k.value for k, _ in root.value]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        failures.append("%s: %s appears more than once" % (path, dup))
    for k, v in root.value:
        if k.value == "attachment_surface_z":
            plane = r.num(v, "attachment_surface_z")
            continue
        if not isinstance(v, yaml.MappingNode):
            continue
        fields = [r.num(r.get(v, f), "%s %s" % (k.value, f)) if r.get(v, f) is not None
                  else None for f in ("index", "type", "z", "height")]
        if None in fields:
            r.fail(v, "layer %s lacks a number in index, type, z or height" % k.value)
            continue
        index, dtype, z, height = fields
        at = (int(index), int(dtype))
        if at in layers:
            r.fail(v, "layer %s is at %d/%d, as %s is" % (k.value, at[0], at[1], layers[at][0]))
            continue
        layers[at] = (k.value, r.text(v, "purpose"), z, height)
    failures += r.failures
    return plane, layers


def studio_name(pdk_name):
    for suffix, theirs in STUDIO_NAMES:
        if pdk_name.endswith(suffix) and len(pdk_name) > len(suffix):
            return pdk_name[:-len(suffix)] + theirs
    return pdk_name


def same_bytes(copy, source, what, failures):
    if not os.path.isfile(copy) or os.path.islink(copy):
        failures.append("%s: %s is missing or not a regular file" % (copy, what))
        return
    a, b = sha256(copy), sha256(source)
    if a != b:
        failures.append("%s: %s is not %s byte for byte (sha256 %s, the PDK's %s)"
                        % (copy, what, source, a[:12], b[:12]))


def check_copies(pdk, trees, key):
    failures = []
    pdk = pathlib.Path(pdk)
    same_bytes(pathlib.Path(trees[PLUGIN]) / PLUGIN_DESCRIPTOR, pdk / KEY,
               "the plugin's test descriptor", failures)
    studio = pathlib.Path(trees[STUDIO])
    same_bytes(studio / STUDIO_FIXTURE, pdk / PDK_STACKUP, "Studio's copy of the PDK stackup",
               failures)
    bundled = studio / STUDIO_BUNDLED
    _, theirs = stackup_layers(pdk / PDK_STACKUP, failures)
    plane, ours = stackup_layers(bundled, failures)
    if not theirs:
        failures.append("%s: no layers, so there is nothing to compare" % (pdk / PDK_STACKUP))
    for at, (name, purpose, z, height) in sorted(theirs.items()):
        where = "%s: %s %d/%d" % (bundled, name, at[0], at[1])
        if at not in ours:
            failures.append("%s is missing" % where)
            continue
        oname, opurpose, oz, oheight = ours[at]
        if oname != studio_name(name):
            failures.append("%s is named %s, Studio's name for it is %s"
                            % (where, oname, studio_name(name)))
        if opurpose != purpose:
            failures.append("%s has purpose %s, the PDK's %s" % (where, opurpose, purpose))
        if oz != z:
            failures.append("%s has z %s, the PDK's %s" % (where, oz, z))
        if oheight != height:
            failures.append("%s has height %s, the PDK's %s" % (where, oheight, height))
    for at, (oname, opurpose, _, _) in sorted(ours.items()):
        if at not in theirs and opurpose != "render_only":
            failures.append("%s: %s %d/%d is not in the PDK's stackup and is not marked "
                            "render_only" % (bundled, oname, at[0], at[1]))
    if plane != key:
        failures.append("%s: attachment_surface_z is %s, the PDK's plane is %s"
                        % (bundled, plane, shortest(key)))
    summary = ("the plugin's descriptor and Studio's stackup copy are the PDK's bytes; Studio's "
               "bundled stackup agrees on %d layers" % len(theirs))
    return failures, summary


def check_named_copies(pdk, copies):
    failures = []
    for label, path in sorted(copies.items()):
        same_bytes(pathlib.Path(path), pathlib.Path(pdk) / KEY,
                   "%s's copy of the PDK stack descriptor" % label, failures)
    return failures, "named copies of the key file compared: %s" % (
        ", ".join(sorted(copies)) if copies else "none")


def labelled(specs, option, form):
    """{label: path} from LABEL=PATH strings, or Problem."""
    out = {}
    for spec in specs:
        label, sep, path = spec.partition("=")
        if not sep or not label or not path:
            raise Problem("%s takes %s, not %s" % (option, form, spec))
        if label in out:
            raise Problem("%s %s given twice" % (option, label))
        out[label] = path
    return out


def check(pdk, interconnect, trees, exemptions, copies=None):
    """(failures, notes, summary lines)."""
    missing = [t for t in TREES if t not in trees]
    unknown = [t for t in trees if t not in TREES]
    failures = ["no --tree for %s" % t for t in missing]
    failures += ["%s is not one of the walked trees (%s)" % (t, ", ".join(TREES)) for t in unknown]
    failures += ["%s=%s is not a directory" % (t, d) for t, d in sorted(trees.items())
                 if t in TREES and not os.path.isdir(d)]
    if failures:
        return failures, [], []
    try:
        key = read_key(pdk)
        methods = read_methods(interconnect)
    except Problem as e:
        return [str(e)], [], []
    f1, notes, s1 = check_files(trees, key, methods, exemptions)
    f2, s2 = check_copies(pdk, trees, key)
    f3, s3 = check_named_copies(pdk, copies or {})
    return f1 + f2 + f3, notes, ["attach plane %s from %s" % (shortest(key),
                                                              pathlib.Path(pdk) / KEY),
                                 s1, s2, s3]


# --- self-test ----------------------------------------------------------------

def self_test():
    results = []

    def expect(name, ok):
        results.append((name, ok))

    KEYFILE = {"beol": {"attachment_surface_z_um": "@10.8103@", "metals": []}}
    STACKUP = (
        "attachment_surface_z: 10.8103\n"
        "Metal4:\n  index: 50\n  type: 0\n  z: 0.64\n  height: 0.49\n"
        "Metal4.filler:\n  index: 50\n  type: 22\n  purpose: filler\n  z: 0.64\n  height: 0.49\n"
        "Passiv.pillar:\n  index: 9\n  type: 35\n  z: 7.8103\n  height: 3.0\n")
    BUNDLED = (
        "attachment_surface_z: 10.8103\n"
        "Metal4:\n  index: 50\n  type: 0\n  z: 0.64\n  height: 0.49\n"
        "Metal4Filler:\n  index: 50\n  type: 22\n  purpose: filler\n  z: 0.640\n  height: 0.49\n"
        "Passiv:\n  index: 9\n  type: 0\n  purpose: render_only\n  z: 10.8103\n  height: 1.90\n"
        "Passiv_pillar:\n  index: 9\n  type: 35\n  z: 7.8103\n  height: 3.00\n")
    METHODS_JSON = {"methods": {
        "opt1": {"connection_stack": {"layers": [{"name": "CuPillar", "height": 28.0},
                                                 {"name": "SnAgCap", "height": 16.0}]}},
        "bump": {"connection_stack": {"layers": [{"name": "Ball", "height": 80.0}]}}}}
    # A copy of the key file in a tree the check does not walk, named by --pdk-copy.
    STAGED = "system/tests/fixtures/interposer_pdk/" + KEY

    def chiplet(plane="10.8103", z1="54.8103", z2="34.8103", tech="intm4tm2",
                stacks=True, extra=""):
        s = "format_version: '1.0'\n"
        if stacks:
            s += ("connection_stacks:\n  micro:\n    layers:\n    - name: Cu\n"
                  "      height: 18.0\n    - name: Cap\n      height: 6.0\n")
        s += "components:\n- id: interposer\n  type: interposer\n"
        if tech:
            s += "  technology: %s\n" % tech
        s += "  position:\n    z: 0.0\n"
        if plane is not None:
            s += "  attachment_surface_z: %s\n" % plane
        s += ("- id: U1\n  type: die\n  connection: opt1\n  position:\n    z: %s\n"
              "- id: U2\n  type: die\n  connection: %s\n  position:\n    z: %s\n"
              % (z1, "micro" if stacks else "bump", z2))
        return s + extra

    def build(t):
        pdk, ic = t / "pdk", t / "ic"
        files = {
            pdk / KEY: json.dumps(KEYFILE).replace('"@10.8103@"', "10.8103"),
            pdk / PDK_STACKUP: STACKUP,
            ic / METHODS: json.dumps(METHODS_JSON),
            t / "plugin" / PLUGIN_DESCRIPTOR: None,
            t / "plugin/tests/two/design.chiplet": chiplet(plane=None, stacks=False,
                                                           z2="90.8103"),
            t / "studio" / STUDIO_FIXTURE: STACKUP,
            t / "studio" / STUDIO_BUNDLED: BUNDLED,
            t / "studio/tests/fixtures/smoke.chiplet": chiplet(z1="0", z2="0.0"),
            t / "studio/tests/fixtures/other.chiplet": chiplet(plane="13.83", z1="57.83",
                                                               tech="sg13g2"),
            t / "studio/tests/fixtures/invalid_yaml.chiplet": "a: [\n",
            t / "studio/build/tests/fixtures/old.chiplet": chiplet(plane="13.83"),
            t / "studio/extern/old.chiplet": chiplet(plane="13.83"),
            t / "studio/conformance/fixtures/v1.chiplet": chiplet(plane="13.83"),
            t / "examples/two/outputs/two.chiplet": chiplet(),
            t / "exempt.txt": "# synthetic cases\n",
        }
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if text is not None:
                path.write_text(text)
        for copy in (t / "plugin" / PLUGIN_DESCRIPTOR, t / STAGED):
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes((pdk / KEY).read_bytes())
        trees = {PLUGIN: str(t / "plugin"), STUDIO: str(t / "studio"),
                 EXAMPLES: str(t / "examples")}
        return pdk, ic, trees

    def run(change=None, copies=()):
        with tempfile.TemporaryDirectory() as d:
            t = pathlib.Path(d)
            pdk, ic, trees = build(t)
            if change:
                change(t, trees)
            failures, notes, _ = check(str(pdk), str(ic), trees, str(t / "exempt.txt"),
                                       {label: str(t / rel) for label, rel in copies})
            return failures, notes

    def put(rel, text):
        def change(t, trees):
            p = t / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return change

    def edit(rel, old, new):
        def change(t, trees):
            p = t / rel
            text = p.read_text()
            assert old in text, (rel, old)
            p.write_text(text.replace(old, new, 1))
        return change

    def both(*changes):
        def change(t, trees):
            for c in changes:
                c(t, trees)
        return change

    def fails(name, change, *want, copies=()):
        failures, _ = run(change, copies)
        ok = bool(failures) and all(any(w in f for f in failures) for w in want)
        expect(name, ok)
        if not ok:
            print("  got: %r" % failures)

    def passes(name, change=None, copies=()):
        failures, _ = run(change, copies)
        expect(name, not failures)
        if failures:
            print("  got: %r" % failures)

    def refused(name, specs, want):
        try:
            labelled(specs, "--pdk-copy", "LABEL=PATH")
            got = None
        except Problem as e:
            got = str(e)
        ok = got is not None and want in got
        expect(name, ok)
        if not ok:
            print("  got: %r" % got)

    EX = "examples/two/outputs/two.chiplet"
    SMOKE = "studio/tests/fixtures/smoke.chiplet"
    NAMED = (("system", STAGED),)
    passes("the files and copies as they should be pass")
    fails("a declared plane other than the key fails, naming file and line",
          edit(EX, "attachment_surface_z: 10.8103", "attachment_surface_z: 13.83"),
          "examples/two/outputs/two.chiplet:15: attachment_surface_z is 13.83")
    fails("a die on the old plane fails", edit(EX, "z: 54.8103", "z: 57.83"),
          "two.chiplet:20: U1 z is 57.83, the plane 10.8103 plus opt1's 44 is written 54.8103")
    fails("a die z written as a binary float sum fails",
          edit(EX, "z: 54.8103", "z: 54.810299999999998"), "U1 z is 54.810299999999998")
    fails("a die z with a trailing zero fails", edit(EX, "z: 54.8103", "z: 54.81030"),
          "U1 z is 54.81030")
    passes("a whole sum written N or N.0 passes", both(
        edit(EX, "height: 18.0", "height: 18.1897"),
        edit(EX, "z: 34.8103", "z: 35.0"),
        put("studio/tests/fixtures/whole.chiplet",
            chiplet().replace("height: 18.0", "height: 18.1897").replace("z: 34.8103", "z: 35"))))
    fails("a die off its plane in a file that declares none is held to the key",
          edit("plugin/tests/two/design.chiplet", "z: 54.8103", "z: 57.83"),
          "U1 z is 57.83, the plane 10.8103 plus opt1's 44 is written 54.8103")
    passes("a sum binary floats get wrong is held to the exact decimal",
           put("studio/tests/fixtures/eighteen.chiplet",
               chiplet().replace("    - name: Cap\n      height: 6.0\n", "").replace("height: 18.0", "height: 18")
               .replace("z: 34.8103", "z: 28.8103")))
    fails("a connection from the interconnect PDK is summed from its heights",
          edit("plugin/tests/two/design.chiplet", "z: 90.8103", "z: 93.83"),
          "plus bump's 80 is written 90.8103")
    fails("a connection defined nowhere fails",
          edit(EX, "connection: opt1", "connection: opt9"), "connection opt9 is in neither")
    fails("a stack layer without a height fails", edit(EX, "      height: 6.0\n", ""),
          "a layer of connection stack micro has no height")
    fails("a file on the interposer adapter alone is checked",
          put("plugin/tests/two/draft.chiplet", "interposer:\n  adapter: intm4tm2\n"
              + chiplet(plane="13.83", tech=None)), "draft.chiplet", "is 13.83")
    fails("a quoted plane is not a number", edit(EX, "attachment_surface_z: 10.8103",
                                                 "attachment_surface_z: '10.8103'"),
          "attachment_surface_z is not a number")
    fails("a plane given twice fails", edit(EX, "  attachment_surface_z: 10.8103\n",
                                            "  attachment_surface_z: 10.8103\n"
                                            "  attachment_surface_z: 13.83\n"),
          "attachment_surface_z appears more than once")
    fails("two interposers fail", edit(EX, "- id: U1", "- id: i2\n  type: interposer\n- id: U1"),
          "2 interposer components")
    fails("an unparseable file naming intm4tm2 fails",
          put("studio/tests/fixtures/broken.chiplet", "technology: intm4tm2\na: [\n"),
          "broken.chiplet: does not parse as YAML and names intm4tm2")
    fails("a build/ outside Studio is walked",
          put("plugin/build/old.chiplet", chiplet(plane="13.83")),
          PLUGIN + "/build/old.chiplet:15: attachment_surface_z is 13.83")
    fails("a conformance directory other than conformance/fixtures is walked",
          put("studio/conformance/cases/v1.chiplet", chiplet(plane="13.83")),
          "conformance/cases/v1.chiplet")
    fails("a symlink named .chiplet fails",
          lambda t, trees: os.symlink(t / EX, t / "studio/tests/fixtures/link.chiplet"),
          "tests/fixtures/link.chiplet: is a symlink")
    fails("a tree with no intm4tm2 file fails",
          lambda t, trees: (t / EX).unlink(), "examples (", "no intm4tm2 .chiplet file")
    fails("a missing tree fails", lambda t, trees: trees.pop(STUDIO), "no --tree for "
          + STUDIO)
    fails("an unknown tree fails", lambda t, trees: trees.update({"tools/x": str(t)}),
          "tools/x is not one of the walked trees")
    fails("a key without the plane fails",
          put("pdk/" + KEY, json.dumps({"beol": {}})), "no beol.attachment_surface_z_um")
    bad = both(edit(SMOKE, "attachment_surface_z: 10.8103", "attachment_surface_z: 13.83"))
    exempt = put("exempt.txt", STUDIO + "/tests/fixtures/smoke.chiplet  synthetic rebase case\n")
    passes("an exempted file may keep its own plane", both(bad, exempt))
    fails("an exemption whose file now passes is stale", exempt,
          "stale exemption, tools/chiplet-studio/tests/fixtures/smoke.chiplet now matches")
    fails("an exemption whose file is gone is stale",
          both(exempt, lambda t, trees: (t / SMOKE).unlink()), "no longer exists")
    fails("an exemption for a file not on intm4tm2 is stale",
          put("exempt.txt", STUDIO + "/tests/fixtures/other.chiplet  not ours\n"),
          "is not an intm4tm2 file")
    fails("an exemption without a reason fails",
          both(bad, put("exempt.txt", STUDIO + "/tests/fixtures/smoke.chiplet\n")),
          "gives no reason")
    fails("an exemption given twice fails",
          both(bad, put("exempt.txt", (STUDIO + "/tests/fixtures/smoke.chiplet  a\n") * 2)),
          "is exempted twice")
    fails("an exemption outside the trees fails",
          put("exempt.txt", "tools/interposer-pnr/x.chiplet  frozen copy\n"),
          "is not under any of the walked trees")
    fails("the plugin's descriptor one byte off fails",
          edit("plugin/" + PLUGIN_DESCRIPTOR, "10.8103", "10.8104"),
          "the plugin's test descriptor is not")
    fails("Studio's stackup fixture one byte off fails",
          edit("studio/" + STUDIO_FIXTURE, "height: 3.0", "height: 3.00"),
          "Studio's copy of the PDK stackup is not")
    BUN = "studio/" + STUDIO_BUNDLED
    fails("a bundled layer at another z fails", edit(BUN, "z: 0.640", "z: 0.65"),
          "Metal4.filler 50/22 has z 0.65")
    fails("a bundled layer of another height fails",
          edit(BUN, "z: 7.8103\n  height: 3.00", "z: 7.8103\n  height: 2.8"),
          "Passiv.pillar 9/35 has height 2.8")
    fails("a bundled layer under the PDK's name fails", edit(BUN, "Metal4Filler:", "Metal4F:"),
          "is named Metal4F, Studio's name for it is Metal4Filler")
    fails("a bundled layer of another purpose fails",
          edit(BUN, "type: 22\n  purpose: filler", "type: 22\n  purpose: fill"),
          "has purpose fill")
    fails("a PDK layer missing from the bundled file fails",
          edit(BUN, "Metal4:\n  index: 50\n  type: 0\n  z: 0.64\n  height: 0.49\n", ""),
          "Metal4 50/0 is missing")
    fails("a layer only Studio has must be render_only",
          edit(BUN, "  purpose: render_only\n", ""), "Passiv 9/0 is not in the PDK's stackup")
    fails("a bundled plane other than the key fails",
          edit(BUN, "attachment_surface_z: 10.8103", "attachment_surface_z: 13.83"),
          "attachment_surface_z is 13.83, the PDK's plane is 10.8103")
    fails("two bundled layers on one layer/datatype fail",
          edit(BUN, "Passiv_pillar:\n  index: 9\n  type: 35",
               "Passiv_pillar:\n  index: 9\n  type: 35\n  z: 7.8103\n  height: 3.00\n"
               "Recog_pillar:\n  index: 9\n  type: 35"), "is at 9/35, as Passiv_pillar is")
    passes("a named copy of the key file with the PDK's bytes passes", copies=NAMED)
    fails("a named copy one byte off fails, naming its label and path",
          edit(STAGED, "10.8103", "10.8104"),
          STAGED + ": system's copy of the PDK stack descriptor is not", copies=NAMED)
    fails("a named copy that is missing fails",
          lambda t, trees: (t / STAGED).unlink(),
          STAGED + ": system's copy of the PDK stack descriptor is missing", copies=NAMED)
    fails("a named copy that is a symlink to the key file fails",
          lambda t, trees: ((t / STAGED).unlink(), os.symlink(t / "pdk" / KEY, t / STAGED)),
          "system's copy of the PDK stack descriptor is missing or not a regular file",
          copies=NAMED)
    passes("a copy not named is not compared", edit(STAGED, "10.8103", "10.8104"))
    refused("a copy given without a label is refused", ["x.json"],
            "--pdk-copy takes LABEL=PATH, not x.json")
    refused("a copy given an empty label is refused", ["=x.json"], "takes LABEL=PATH")
    refused("a label given twice is refused", ["a=x.json", "a=y.json"], "--pdk-copy a given twice")

    for name, ok in results:
        print("%-72s %s" % (name, "ok" if ok else "FAILED"))
    bad_count = sum(1 for _, ok in results if not ok)
    print("%d/%d self-test cases pass" % (len(results) - bad_count, len(results)))
    return 1 if bad_count else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdk", help="the interposer PDK root (holds %s)" % KEY)
    ap.add_argument("--interconnect", help="the interconnect PDK root (holds %s)" % METHODS)
    ap.add_argument("--tree", action="append", default=[], metavar="LABEL=DIR",
                    help="a walked tree: %s" % ", ".join(TREES))
    ap.add_argument("--exemptions", help="the exemption file")
    ap.add_argument("--pdk-copy", action="append", default=[], metavar="LABEL=PATH",
                    help="a further copy of %s that must be the PDK's, byte for byte" % KEY)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not (args.pdk and args.interconnect and args.exemptions):
        ap.error("--pdk, --interconnect and --exemptions are required")
    try:
        trees = labelled(args.tree, "--tree", "LABEL=DIR")
        copies = labelled(args.pdk_copy, "--pdk-copy", "LABEL=PATH")
    except Problem as e:
        ap.error(str(e))
    failures, notes, summary = check(args.pdk, args.interconnect, trees, args.exemptions, copies)
    for line in summary:
        print(line)
    for n in notes:
        print(n)
    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        return 1
    print("every IntM4TM2 .chiplet under the walked trees sits on the PDK's plane, and the "
          "consumers' copies of the PDK's files agree with it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
