#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for nix-tail/tail_eval.py, without Nix.

The fixture is what `nix eval` and `nix derivation show` print for a base
checkout and a new one, written by hand in the shape Nix 2.30 prints for
mkShellNoCC shells and a closureInfo (structured attributes, which the show
prints as "structuredAttrs" and leaves only `out` in env): a cli shell, a
build shell that is the cli shell plus LibreLane and the PDK, and in the new
checkout a tail shell built from the cli shell the way nixpkgs' mkShell merges
inputsFrom. Its tail-wrappers is a runCommandLocal whose build command
substitutes this checkout's templates (nix-tail/<name>.in, kept as the check
keeps them), and whose carrier script is a stand-in, the same bytes as the
checkout's copy the check keeps. Each refusal breaks exactly one thing against
the fixture that passes.

RealShows runs the reader on what check-tail-eval.sh --keep kept from Nix
2.30.1 for this repository (tests/nix-tail/nix-2.30.1, two earlier commits of
it, named in revs.json; the JSON files unedited), so the shape the fixture
assumes is also checked against the shape Nix printed. That check kept no
store files: its new.sources/ holds the four store files the kept
tail-wrappers derivation names, copied from the same host's store after the
run, and new.carrier-drc is bin/adk-verify-carrier-drc as it stood at the
newer commit, which is what the check keeps now. It is a frozen snapshot: the
script in bin/ has moved on since, and the fixture stays as recorded.
Its refusals edit those bytes the way a rebinding of tail-wrappers that
nix-tail/check-tail-wrappers.sh does not see would change them.

`--write-fixture <dir> <base rev> <new rev>` writes the fixture as files for
nix-tail/test-check-tail-eval.sh, which serves them from a stand-in nix, with
the store files the check copies under <dir>/store. `--write-carrier <file>`
writes the stand-in carrier script.

Usage: python3 nix-tail/test_tail_eval.py
"""

import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tail_eval  # noqa: E402

# What the override test-check-tail-wrappers.sh binds appends to the build command.
with open(os.path.join(HERE, "..", "tests", "nix-tail", "strip-p-command")) as _handle:
    STRIP_P = _handle.read().rstrip("\n")

BASE_REV = "b" * 40
NEW_REV = "c" * 40
TAIL_HOOK = 'source "$ADK_NIX_TAIL_LIB/tail-env.sh"\n'
BUILDER = "/bin/bash"
BUILDER_ARGS = ["-e", "/nix/store/" + "d" * 32 + "-default-builder.sh"]
STDENV = "/nix/store/" + "s" * 32 + "-stdenv"
WRAPPERS = ("chiplet-export", "adk-verify-carrier-drc", "adk-drc")
CARRIER = b"#!/usr/bin/env bash\n# stands in for bin/adk-verify-carrier-drc\n"
counter = [0]


def sp(name, drv=False):
    """A fresh store path."""
    counter[0] += 1
    return f"/nix/store/{counter[0]:032d}-{name}" + (".drv" if drv else "")


def inputs(*drvs):
    return {d: {"dynamicOutputs": {}, "outputs": ["out"]} for d in drvs}


def package(name, *needs):
    return sp(name, True), {"args": ["-e", "builder.sh"], "builder": "/bin/bash", "name": name,
                            "system": "x86_64-linux", "inputSrcs": [], "inputDrvs": inputs(*needs),
                            "env": {"name": name, "out": sp(name)},
                            "outputs": {"out": {"path": None}}}


def finish(drv):
    drv["outputs"]["out"]["path"] = drv["env"]["out"]
    return drv


def wrappers_package(export_drv, export_out):
    """runCommandLocal "adk-tail-wrappers" as the flake's binding evaluates,
    and the store files it names: {path: bytes}."""
    templates = {name: sp(name + ".in") for name in WRAPPERS}
    carrier, bash = sp("adk-verify-carrier-drc"), sp("bash-interactive-5.3p3")
    command = 'mkdir -p "$out/bin"\n'
    for name in WRAPPERS:
        command += (f'substitute {templates[name]} "$out/bin/{name}" \\\n'
                    f"  --subst-var-by bash {bash}/bin/bash \\\n"
                    f"  --subst-var-by exportPython {export_out} \\\n"
                    f"  --subst-var-by carrierDrc {carrier}\n")
    command += ("\nif grep -n '@[A-Za-z][A-Za-z]*@' \"$out\"/bin/*; then\n"
                '  echo "a placeholder is left in a wrapper" >&2\n'
                "  exit 1\n"
                "fi\n"
                'chmod 0555 "$out"/bin/*\n')
    out = sp("adk-tail-wrappers")
    env = {"__structuredAttrs": "", "allowSubstitutes": "", "buildCommand": command, "buildInputs": "",
           "builder": BUILDER, "cmakeFlags": "", "configureFlags": "", "depsBuildBuild": "",
           "depsBuildBuildPropagated": "", "depsBuildTarget": "", "depsBuildTargetPropagated": "",
           "depsHostHost": "", "depsHostHostPropagated": "", "depsTargetTarget": "",
           "depsTargetTargetPropagated": "", "doCheck": "", "doInstallCheck": "", "enableParallelBuilding": "1",
           "enableParallelChecking": "1", "enableParallelInstalling": "1", "mesonFlags": "",
           "name": "adk-tail-wrappers", "nativeBuildInputs": "", "out": out, "outputs": "out",
           "passAsFile": "buildCommand", "patches": "", "preferLocalBuild": "1", "propagatedBuildInputs": "",
           "propagatedNativeBuildInputs": "", "stdenv": STDENV, "strictDeps": "", "system": "x86_64-linux"}
    files = {carrier: CARRIER}
    for name, path in templates.items():
        with open(os.path.join(HERE, name + ".in"), "rb") as handle:
            files[path] = handle.read()
    drv = {"args": list(BUILDER_ARGS), "builder": BUILDER, "name": "adk-tail-wrappers", "system": "x86_64-linux",
           "inputSrcs": sorted(set(templates.values()) | {carrier, BUILDER_ARGS[1]}),
           "inputDrvs": inputs(export_drv), "env": env, "outputs": {"out": {"path": out}}}
    return (sp("adk-tail-wrappers", True), drv), files


def closure_info(roots):
    out = sp("closure-info")
    return sp("closure-info", True), {
        "args": ["-e", "builder.sh"], "builder": "/bin/bash", "name": "closure-info",
        "system": "x86_64-linux", "inputSrcs": [], "inputDrvs": inputs(),
        "env": {"out": out},
        "structuredAttrs": {"exportReferencesGraph": {"closure": roots}, "name": "closure-info",
                            "out": out},
        "outputs": {"out": {"path": out}}}


def shell(env, drvs, srcs, ci_drv, ci_out, hook, native):
    out = sp("nix-shell")
    e = {"name": "nix-shell", "builder": BUILDER, "buildPhase": "echo ok >> $out",
         "phases": "buildPhase", "buildInputs": "", "stdenv": STDENV,
         "nativeBuildInputs": " ".join(native), "shellHook": hook,
         "ADK_NIX_CLOSURE_INFO": ci_out, "out": out}
    e.update(env)
    return {"args": list(BUILDER_ARGS), "builder": BUILDER, "name": "nix-shell", "system": "x86_64-linux",
            "inputSrcs": sorted(srcs), "inputDrvs": inputs(*drvs, ci_drv), "env": e,
            "outputs": {"out": {"path": out}}}


def fixture(base_rev=BASE_REV, new_rev=NEW_REV):
    """{"revs", "base": {"attrs", "show", "show-ci"}, "new": {...}}."""
    tools = {name: package(name) for name in ("openroad", "klayout", "git", "coreutils", "gawk",
                                              "cli-env", "librelane", "librelane-python")}
    fork = package("kicad-base")
    pdk = package("ihp-open-pdk")
    export = package("adk-tail-export-python", fork[0])
    for _, drv in list(tools.values()) + [fork, pdk, export]:
        finish(drv)
    wrappers, files = wrappers_package(export[0], export[1]["env"]["out"])
    o = {name: drv["env"]["out"] for name, (_, drv) in tools.items()}
    native = [o[n] for n in ("openroad", "klayout", "git", "coreutils", "gawk")]
    lib, lock, tail_lib = sp("nix"), sp("flake.lock"), sp("nix-tail")
    stdenv_srcs = {sp("source-stdenv.sh"), sp("default-builder.sh")}
    cli_roots = [o["cli-env"]] + native
    cli_hook = 'source "$ADK_NIX_LIB/cli-env.sh"\n'
    cli_drvs = [tools[n][0] for n in ("openroad", "klayout", "git", "coreutils", "gawk", "cli-env")]
    fork_out, pdk_out = fork[1]["env"]["out"], pdk[1]["env"]["out"]
    export_out, wrappers_out = export[1]["env"]["out"], wrappers[1]["env"]["out"]

    def side(rev, with_tail):
        cli_env = {"ADK_NIX_FLAKE_REV": rev, "ADK_NIX_LIB": lib, "ADK_NIX_FLAKE_LOCK": lock,
                   "ADK_NIX_PYTHON": sp("python3-3.13.9"), "ADK_NIX_KLAYOUT": o["klayout"],
                   "ADK_NIX_OPENROAD": o["openroad"]}
        ci = {}
        drvs = {}
        cli_ci = closure_info(cli_roots)
        # The shells' own paths do not depend on the revision here except
        # through out, which is all the check may see change.
        cli = shell(cli_env, cli_drvs, stdenv_srcs | {lib, lock}, cli_ci[0],
                    cli_ci[1]["env"]["out"], cli_hook, native)
        build_ci = closure_info(cli_roots + [o["librelane"], o["librelane-python"], pdk_out])
        build = shell(dict(cli_env, ADK_NIX_SG13G2_PDK=pdk_out, ADK_NIX_LIBRELANE=o["librelane"]),
                      cli_drvs + [tools["librelane"][0], tools["librelane-python"][0], pdk[0]],
                      stdenv_srcs | {lib, lock}, build_ci[0], build_ci[1]["env"]["out"],
                      cli_hook + "\n" + 'source "$ADK_NIX_LIB/build-env.sh"\n', native)
        ci[cli_ci[0]], ci[build_ci[0]] = cli_ci[1], build_ci[1]
        shells = {"cli": sp("nix-shell", True), "build": sp("nix-shell", True)}
        drvs[shells["cli"]], drvs[shells["build"]] = cli, build
        packages = {"kicad-fork-base": fork[0], "sg13g2-pdk": pdk[0]}
        drvs[fork[0]], drvs[pdk[0]] = fork[1], pdk[1]
        if with_tail:
            tail_ci = closure_info(cli_roots + [wrappers_out, export_out, fork_out, pdk_out])
            tail = shell(dict(cli_env, ADK_NIX_TAIL_WRAPPERS=wrappers_out,
                              ADK_NIX_TAIL_EXPORT_PYTHON=export_out, ADK_NIX_KICAD_FORK_BASE=fork_out,
                              ADK_NIX_SG13G2_PDK=pdk_out, ADK_NIX_TAIL_LIB=tail_lib),
                         cli_drvs + [wrappers[0], export[0], fork[0], pdk[0]],
                         stdenv_srcs | {lib, lock, tail_lib}, tail_ci[0], tail_ci[1]["env"]["out"],
                         cli_hook + "\n" + TAIL_HOOK, [wrappers_out] + native)
            ci[tail_ci[0]] = tail_ci[1]
            shells["tail"] = sp("nix-shell", True)
            drvs[shells["tail"]] = tail
            packages.update({"tail-wrappers": wrappers[0], "tail-export-python": export[0]})
            drvs[wrappers[0]], drvs[export[0]] = wrappers[1], export[1]
        return {"attrs": {"shells": shells, "packages": packages}, "show": drvs, "show-ci": ci,
                "sources": dict(files) if with_tail else {}, "carrier": CARRIER}

    base = side(base_rev, False)
    new = side(new_rev, True)
    # The unchanged shells: identical but for the revision and their out path.
    for name in ("cli", "build"):
        b = base["show"][base["attrs"]["shells"][name]]
        n = new["show"][new["attrs"]["shells"][name]]
        keep_rev, keep_out = b["env"]["ADK_NIX_FLAKE_REV"], b["env"]["out"]
        base["show"][base["attrs"]["shells"][name]] = b = copy.deepcopy(n)
        b["env"]["ADK_NIX_FLAKE_REV"], b["env"]["out"] = keep_rev, keep_out
        b["outputs"]["out"]["path"] = keep_out
        base["show-ci"] = {k: v for k, v in new["show-ci"].items()
                           if k in {x for s in base["show"].values() for x in s["inputDrvs"]}}
    return {"revs": {"base": base_rev, "new": new_rev}, "base": base, "new": new}


def write(where, fx, store=None):
    """The files compare reads; with store, the store files go there for the
    check to copy, and the checkout's carrier copy, which the check takes
    from the checkout, is not written."""
    with open(os.path.join(where, "revs.json"), "w") as handle:
        json.dump(fx["revs"], handle)
    into = store or os.path.join(where, "new.sources")
    os.makedirs(into, exist_ok=True)
    for path, data in fx["new"]["sources"].items():
        with open(os.path.join(into, path[len("/nix/store/"):]), "wb") as handle:
            handle.write(data)
    if store is None and fx["new"]["carrier"] is not None:
        with open(os.path.join(where, "new.carrier-drc"), "wb") as handle:
            handle.write(fx["new"]["carrier"])
    everything = {}
    for side in ("base", "new"):
        for part in ("attrs", "show", "show-ci"):
            with open(os.path.join(where, f"{side}.{part}.json"), "w") as handle:
                json.dump(fx[side][part], handle, indent=1)
        for part in ("shells", "packages"):
            with open(os.path.join(where, f"{side}.{part}.json"), "w") as handle:
                json.dump(fx[side]["attrs"][part], handle)
        everything.update(fx[side]["show"])
        everything.update(fx[side]["show-ci"])
    with open(os.path.join(where, "all.show.json"), "w") as handle:
        json.dump(everything, handle, indent=1)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = fixture()

    def shell(self, side, name):
        return self.fx[side]["show"][self.fx[side]["attrs"]["shells"][name]]

    def pkg(self, side, name):
        return self.fx[side]["show"][self.fx[side]["attrs"]["packages"][name]]

    def wrap(self):
        return self.pkg("new", "tail-wrappers")

    def template(self, name):
        return next(p for p in self.fx["new"]["sources"] if p.endswith(f"-{name}.in"))

    def edit_template(self, name, old, new):
        path = self.template(name)
        data = self.fx["new"]["sources"][path]
        self.assertIn(old, data)
        self.fx["new"]["sources"][path] = data.replace(old, new)

    def ci(self, name):
        drv = self.shell("new", name)
        path = next(d for d in drv["inputDrvs"] if d.endswith("-closure-info.drv"))
        return self.fx["new"]["show-ci"][path]

    def run_compare(self):
        write(self.tmp.name, self.fx)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tail_eval.main(["compare", self.tmp.name])
        return code, err.getvalue()

    def bash(self, name):
        """@bash@ filled with a store path named name in every wrapper."""
        env = self.wrap()["env"]
        old = next(p for p in tail_eval.STORE_WORD.findall(env["buildCommand"])
                   if p.endswith("-bash-interactive-5.3p3"))
        env["buildCommand"] = env["buildCommand"].replace(old, sp(name))

    def refused(self, pattern):
        code, err = self.run_compare()
        self.assertEqual(code, 1, err)
        self.assertRegex(err, pattern)
        return err


class Accepted(Case):
    def test_fixture_passes(self):
        code, err = self.run_compare()
        self.assertEqual(code, 0, err)
        self.assertIn(f"cli and build differ from {BASE_REV} only in the revision they record", err)
        self.assertIn("tail = cli + tail-wrappers + tail-export-python + kicad-fork-base + sg13g2-pdk", err)

    def test_same_revision_passes(self):
        self.fx = fixture(NEW_REV, NEW_REV)
        code, err = self.run_compare()
        self.assertEqual(code, 0, err)

    def test_a_first_line_of_255_bytes_passes(self):
        # 2 + 11 + 32 + 1 + 197 + 12: the longest first line Linux reads whole
        # (since 5.1).
        self.bash("bash-" + "x" * 192)
        code, err = self.run_compare()
        self.assertEqual(code, 0, err)

    def test_later_nix_format_passes(self):
        # Wrapped in "derivations", keyed by the base name.
        for side in ("base", "new"):
            for part in ("show", "show-ci"):
                self.fx[side][part] = {"derivations": {k[len("/nix/store/"):]: v
                                                       for k, v in self.fx[side][part].items()},
                                       "version": 4}
        code, err = self.run_compare()
        self.assertEqual(code, 0, err)

    def test_drvs_lists_every_attribute(self):
        write(self.tmp.name, self.fx)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tail_eval.main(["drvs", os.path.join(self.tmp.name, "new.attrs.json")])
        self.assertEqual(code, 0)
        attrs = self.fx["new"]["attrs"]
        self.assertEqual(set(out.getvalue().split()),
                         set(attrs["shells"].values()) | set(attrs["packages"].values()))

    def test_closure_info_as_the_drv_file_keeps_it_passes(self):
        # The .drv file's form: the structured attributes as env "__json".
        for name in ("cli", "tail"):
            ci = self.ci(name)
            ci["env"]["__json"] = json.dumps(ci.pop("structuredAttrs"))
        code, err = self.run_compare()
        self.assertEqual(code, 0, err)

    def test_wrapper_sources_lists_the_templates_and_the_carrier(self):
        write(self.tmp.name, self.fx)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tail_eval.main(["wrapper-sources", os.path.join(self.tmp.name, "new.attrs.json"),
                                   os.path.join(self.tmp.name, "new.show.json")])
        self.assertEqual(code, 0)
        self.assertEqual(set(out.getvalue().split()), set(self.fx["new"]["sources"]))

    def test_closure_infos_lists_the_shells(self):
        write(self.tmp.name, self.fx)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tail_eval.main(["closure-infos", os.path.join(self.tmp.name, "new.show.json")])
        self.assertEqual(code, 0)
        self.assertEqual(set(out.getvalue().split()), set(self.fx["new"]["show-ci"]))


class Refused(Case):
    def test_cli_changed_otherwise(self):
        self.shell("new", "cli")["env"]["ADK_NIX_KLAYOUT"] = "/nix/store/" + "k" * 32 + "-klayout-0.30.5"
        self.refused(r"cli: differs from the base in env\.ADK_NIX_KLAYOUT")

    def test_build_gains_an_input(self):
        self.shell("new", "build")["inputDrvs"].update(inputs(sp("extra", True)))
        self.refused(r"build: differs from the base in inputDrvs\./nix/store/\d+-extra\.drv")

    def test_build_hook_changed(self):
        self.shell("new", "build")["env"]["shellHook"] += "echo\n"
        self.refused(r"build: differs from the base in env\.shellHook")

    def test_revision_not_the_checkout(self):
        self.fx["revs"]["new"] = "d" * 40
        self.refused(r"cli: the new shell records revision c{40}, its checkout is at d{40}")

    def test_tail_drops_a_cli_variable(self):
        del self.shell("new", "tail")["env"]["ADK_NIX_OPENROAD"]
        self.refused(r"tail: does not set the cli shell's ADK_NIX_OPENROAD")

    def test_tail_sets_a_cli_variable_otherwise(self):
        self.shell("new", "tail")["env"]["ADK_NIX_PYTHON"] = sp("python3-3.12")
        self.refused(r"tail: sets ADK_NIX_PYTHON otherwise than the cli shell")

    def test_tail_adds_a_variable(self):
        self.shell("new", "tail")["env"]["PDK_ROOT"] = "/opt/pdks"
        self.refused(r"tail: adds \[.*'PDK_ROOT'.*\], expected")

    def test_tail_names_another_pdk(self):
        self.shell("new", "tail")["env"]["ADK_NIX_SG13G2_PDK"] = sp("ihp-open-pdk")
        self.refused(r"tail: ADK_NIX_SG13G2_PDK is .*, not sg13g2-pdk")

    def test_tail_path_without_wrappers_first(self):
        env = self.shell("new", "tail")["env"]
        words = env["nativeBuildInputs"].split()
        env["nativeBuildInputs"] = " ".join(words[1:] + words[:1])
        self.refused(r"tail: nativeBuildInputs is not tail-wrappers followed by the cli shell's")

    def test_tail_path_with_another_package(self):
        env = self.shell("new", "tail")["env"]
        env["nativeBuildInputs"] += " " + sp("python3")
        self.refused(r"tail: nativeBuildInputs is not")

    def test_tail_hook_otherwise(self):
        env = self.shell("new", "tail")["env"]
        env["shellHook"] = TAIL_HOOK + "\n" + env["shellHook"]
        self.refused(r"tail: shellHook is not the cli shell's followed by")

    def test_tail_extra_source(self):
        self.shell("new", "tail")["inputSrcs"].append(sp("extra-source"))
        self.refused(r"tail: inputSrcs are not the cli shell's plus ADK_NIX_TAIL_LIB")

    def test_tail_extra_input(self):
        self.shell("new", "tail")["inputDrvs"].update(inputs(sp("gcc", True)))
        self.refused(r"tail: input derivations are not .* \(extra \['/nix/store/\d+-gcc\.drv'\]")

    def test_tail_without_the_fork(self):
        del self.shell("new", "tail")["inputDrvs"][self.fx["new"]["attrs"]["packages"]["kicad-fork-base"]]
        self.refused(r"tail: input derivations are not .* missing \['/nix/store/\d+-kicad-base\.drv'\]")

    def test_closure_without_the_pdk(self):
        pdk = self.pkg("new", "sg13g2-pdk")["env"]["out"]
        self.ci("tail")["structuredAttrs"]["exportReferencesGraph"]["closure"].remove(pdk)
        self.refused(r"tail: closure roots are not the cli shell's plus")

    def test_closure_with_an_extra_root(self):
        self.ci("tail")["structuredAttrs"]["exportReferencesGraph"]["closure"].append(sp("gcc"))
        self.refused(r"tail: closure roots .* \(extra \['/nix/store/\d+-gcc'\]\)")

    def test_fork_rebuilt(self):
        self.fx["base"]["attrs"]["packages"]["kicad-fork-base"] = sp("kicad-base", True)
        self.refused(r"kicad-fork-base: .* is not the base's")

    def test_pdk_rebuilt(self):
        self.fx["base"]["attrs"]["packages"]["sg13g2-pdk"] = sp("ihp-open-pdk", True)
        self.refused(r"sg13g2-pdk: .* is not the base's")

    def test_build_shell_names_another_pdk(self):
        for side in ("base", "new"):
            self.shell(side, "build")["env"]["ADK_NIX_SG13G2_PDK"] = "/nix/store/" + "p" * 32 + "-pdk"
        self.refused(r"build: ADK_NIX_SG13G2_PDK is not the tail shell's sg13g2-pdk")

    def test_export_python_not_from_the_fork(self):
        self.pkg("new", "tail-export-python")["inputDrvs"] = inputs(sp("kicad-base", True))
        self.refused(r"tail-export-python: is not built from kicad-fork-base")

    def test_wrappers_not_from_the_export_python(self):
        self.pkg("new", "tail-wrappers")["inputDrvs"] = inputs()
        self.refused(r"tail-wrappers: is not built from tail-export-python")

    def test_tail_builder_otherwise(self):
        self.shell("new", "tail")["builder"] = "/bin/sh"
        self.refused(r"tail: builder is not the cli shell's")

    def tail_lib(self, name):
        tail = self.shell("new", "tail")
        old, new = tail["env"]["ADK_NIX_TAIL_LIB"], sp(name)
        tail["env"]["ADK_NIX_TAIL_LIB"] = new
        tail["inputSrcs"] = sorted(set(tail["inputSrcs"]) - {old} | {new})

    def test_tail_lib_named_otherwise(self):
        self.tail_lib("other")
        self.refused(r"tail: ADK_NIX_TAIL_LIB is /nix/store/\d+-other, not a store path named nix-tail")

    def test_tail_lib_named_with_a_prefix(self):
        self.tail_lib("other-nix-tail")
        self.refused(r"tail: ADK_NIX_TAIL_LIB is /nix/store/\d+-other-nix-tail, not a store path named nix-tail")

    def test_cli_closure_info_variable_otherwise(self):
        self.shell("new", "cli")["env"]["ADK_NIX_CLOSURE_INFO"] = sp("closure-info")
        self.refused(r"cli: ADK_NIX_CLOSURE_INFO is not its closure-info input")

    def test_tail_closure_info_variable_otherwise(self):
        cli = self.shell("new", "cli")["env"]["ADK_NIX_CLOSURE_INFO"]
        self.shell("new", "tail")["env"]["ADK_NIX_CLOSURE_INFO"] = cli
        self.refused(r"tail: ADK_NIX_CLOSURE_INFO is not its closure-info input")


class RefusedWrappers(Case):
    """The tail-wrappers derivation as a rebinding the static check does not
    see would change it."""

    def command(self, old, new, count=1):
        env = self.wrap()["env"]
        self.assertEqual(env["buildCommand"].count(old), count)
        env["buildCommand"] = env["buildCommand"].replace(old, new)

    def test_a_command_after_the_substitution(self):
        self.command('chmod 0555', "sed -i '1s/ -p$//' \"$out\"/bin/*\nchmod 0555")
        self.refused(r"tail-wrappers: the build command's line 19 is 'sed -i .*1s/ -p\$//")

    def test_a_wrapper_without_p(self):
        self.edit_template("chiplet-export", b"#!@bash@ -p\n", b"#!@bash@\n")
        self.refused(r"tail-wrappers: chiplet-export: starts '#!/nix/store/\d+-bash-interactive-5\.3p3/bin/bash', "
                     r"not '#!/nix/store/\d+-bash-interactive-5\.3p3/bin/bash -p'")

    def test_a_first_line_linux_cuts(self):
        self.bash("bash-" + "x" * 193)
        self.refused(r"tail-wrappers: chiplet-export: its first line is 256 bytes, "
                     r"more than the 255 Linux \(since 5\.1\) reads")

    def test_a_trailing_comment(self):
        # Only a line that starts with # is a comment here.
        self.edit_template("adk-drc", b'"$@"\n', b'"$@" # the arguments\n')
        self.refused(r"tail-wrappers: adk-drc: command 3 is '.*\"\$@\" # the arguments', not ")

    def test_a_command_before_the_exec(self):
        self.edit_template("adk-drc", b"exec ", b"echo hi\nexec ")
        self.refused(r"tail-wrappers: adk-drc: command 3 is 'echo hi', not 'exec ")

    def test_another_script(self):
        self.edit_template("chiplet-export", b"chiplet_export_cli.py", b"other_cli.py")
        self.refused(r"tail-wrappers: chiplet-export: command 5 is .*other_cli\.py")

    def test_a_guard_dropped(self):
        self.edit_template("chiplet-export", b': "${PDK_ROOT:?enter through devShells.tail}"\n', b"")
        self.refused(r"tail-wrappers: chiplet-export: command 2 is ': \"\$\{KICAD_CHIPLET_PYTHON")

    def test_the_gds_to_kicad_guard_dropped(self):
        self.edit_template("chiplet-export", b': "${GDS_TO_KICAD_ROOT:?enter through devShells.tail}"\n', b"")
        self.refused(r"tail-wrappers: chiplet-export: command 4 is 'exec .*, not ': \"\$\{GDS_TO_KICAD_ROOT")

    def test_a_character_bash_splits_otherwise(self):
        self.edit_template("adk-drc", b'"$@"\n', b'"$@"\x0b\n')
        self.refused(r"tail-wrappers: adk-drc: holds \['\\x0b'\]")

    def test_another_export_python(self):
        export = self.pkg("new", "tail-export-python")["env"]["out"]
        self.command(export, sp("python3-3.13.9"), 3)
        self.refused(r"tail-wrappers: fills @exportPython@ with /nix/store/\d+-python3-3\.13\.9, "
                     r"not tail-export-python")

    def test_two_bashes(self):
        bash = next(p for p in tail_eval.STORE_WORD.findall(self.wrap()["env"]["buildCommand"])
                    if p.endswith("-bash-interactive-5.3p3"))
        env = self.wrap()["env"]
        env["buildCommand"] = env["buildCommand"].replace(bash, sp("bash-5.3p3"), 1)
        self.refused(r"tail-wrappers: fills @bash@ with \[.*\], not one path")

    def test_the_carrier_is_not_the_checkouts(self):
        self.fx["new"]["carrier"] = b"#!/usr/bin/env bash\nexit 0\n"
        self.refused(r"tail-wrappers: /nix/store/\d+-adk-verify-carrier-drc is not the checkout's")

    def test_the_carrier_is_another_file(self):
        drv = self.wrap()
        old = next(p for p in drv["inputSrcs"] if p.endswith("-adk-verify-carrier-drc"))
        new = sp("run_drc.py")
        self.command(old, new, 3)
        drv["inputSrcs"] = sorted(set(drv["inputSrcs"]) - {old} | {new})
        self.fx["new"]["sources"][new] = self.fx["new"]["sources"].pop(old)
        self.refused(r"tail-wrappers: fills @carrierDrc@ with /nix/store/\d+-run_drc\.py, not a copy")

    def test_templates_swapped(self):
        a, b = self.template("chiplet-export"), self.template("adk-drc")
        self.command(a, "@A@")
        self.command(b, a)
        self.command("@A@", b)
        self.refused(r"tail-wrappers: chiplet-export is made from /nix/store/\d+-adk-drc\.in")

    def test_a_hook(self):
        self.wrap()["env"]["postHook"] = "substitute() { cp \"$1\" \"$2\"; }"
        self.refused(r"tail-wrappers: sets postHook otherwise than runCommandLocal with nothing added")

    def test_a_native_input(self):
        self.wrap()["env"]["nativeBuildInputs"] = sp("gnused")
        self.refused(r"tail-wrappers: sets nativeBuildInputs otherwise than runCommandLocal")

    def test_an_extra_source(self):
        self.wrap()["inputSrcs"].append(sp("other.in"))
        self.refused(r"tail-wrappers: its sources are not .* \(extra \['/nix/store/\d+-other\.in'\]")

    def test_eleven_store_paths(self):
        # A literal <store> reads as one in the skeleton, not as a store path.
        env = self.wrap()["env"]
        carrier = next(p for p in self.wrap()["inputSrcs"] if p.endswith("-adk-verify-carrier-drc"))
        head, tail = env["buildCommand"].rsplit(carrier, 1)
        env["buildCommand"] = head + "<store>" + tail
        self.refused(r"tail-wrappers: the build command names 11 store paths, not 12")

    def test_the_last_call_written_with_a_literal_store(self):
        # Its four paths as <store>: the skeleton still matches, with eight.
        env = self.wrap()["env"]
        head, last = env["buildCommand"].rsplit("\nsubstitute ", 1)
        env["buildCommand"] = head + "\nsubstitute " + tail_eval.STORE_WORD.sub("<store>", last)
        self.refused(r"tail-wrappers: the build command names 8 store paths, not 12")

    def test_other_arguments(self):
        drv = self.wrap()
        old, new = drv["args"][1], sp("other-builder.sh")
        drv["args"] = [drv["args"][0], new]
        drv["inputSrcs"] = sorted(set(drv["inputSrcs"]) - {old} | {new})
        self.refused(r"tail-wrappers: args is not the cli shell's")

    def test_another_system(self):
        self.wrap()["system"] = "aarch64-linux"
        self.refused(r"tail-wrappers: system is not the cli shell's")

    def test_another_name(self):
        self.wrap()["env"]["name"] = "adk-tail-wrappers-2"
        self.refused(r"tail-wrappers: name is 'adk-tail-wrappers-2', not 'adk-tail-wrappers'")

    def test_another_out(self):
        self.wrap()["env"]["out"] = sp("adk-tail-wrappers")
        self.refused(r"tail-wrappers: out is '/nix/store/\d+-adk-tail-wrappers', not '/nix/store/")

    def test_another_builder_variable(self):
        self.wrap()["env"]["builder"] = "/bin/sh"
        self.refused(r"tail-wrappers: builder is '/bin/sh', not ")

    def test_another_system_variable(self):
        self.wrap()["env"]["system"] = "aarch64-linux"
        self.refused(r"tail-wrappers: system is 'aarch64-linux', not 'x86_64-linux'")

    def test_structured_attributes(self):
        self.wrap()["structuredAttrs"] = {"buildCommand": ""}
        self.refused(r"tail-wrappers: has structured attributes")

    def test_another_builder(self):
        self.wrap()["builder"] = "/bin/sh"
        self.refused(r"tail-wrappers: builder is not the cli shell's")

    def test_another_stdenv(self):
        self.wrap()["env"]["stdenv"] = sp("stdenv-linux")
        self.refused(r"tail-wrappers: stdenv is '/nix/store/\d+-stdenv-linux', not")


class Unreadable(Case):
    def test_no_tail_shell(self):
        del self.fx["new"]["attrs"]["shells"]["tail"]
        self.refused(r"unreadable: the new flake has no tail shell")

    def test_no_tail_package(self):
        del self.fx["new"]["attrs"]["packages"]["tail-export-python"]
        self.refused(r"unreadable: the new flake has no package tail-export-python")

    def test_tail_not_shown(self):
        del self.fx["new"]["show"][self.fx["new"]["attrs"]["shells"]["tail"]]
        self.refused(r"unreadable: the tail shell \(.*\) is not in the derivation show")

    def test_closure_info_json_broken(self):
        ci = self.ci("tail")
        del ci["structuredAttrs"]
        ci["env"]["__json"] = "{"
        self.refused(r"unreadable: the tail closure-info: __json is not JSON")

    def test_closure_info_without_its_attributes(self):
        del self.ci("tail")["structuredAttrs"]
        self.refused(r"unreadable: the tail closure-info: no exportReferencesGraph closure")

    def test_closure_info_attributes_not_an_object(self):
        self.ci("tail")["structuredAttrs"]["exportReferencesGraph"] = ["closure"]
        self.refused(r"unreadable: the tail closure-info: no exportReferencesGraph closure")

    def test_two_closure_infos(self):
        self.shell("new", "tail")["inputDrvs"].update(inputs(sp("closure-info", True)))
        self.refused(r"unreadable: the tail shell has 2 closure-info inputs, expected 1")

    def test_show_not_json(self):
        write(self.tmp.name, self.fx)
        with open(os.path.join(self.tmp.name, "new.show.json"), "w") as handle:
            handle.write("error: flake has no attribute\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tail_eval.main(["compare", self.tmp.name])
        self.assertEqual(code, 1)
        self.assertIn("unreadable: ", err.getvalue())

    def test_closure_info_in_both_forms(self):
        ci = self.ci("tail")
        ci["env"]["__json"] = json.dumps(ci["structuredAttrs"])
        self.refused(r"unreadable: the tail closure-info: both structuredAttrs and env __json")

    def test_a_template_not_kept(self):
        del self.fx["new"]["sources"][self.template("adk-drc")]
        self.refused(r"unreadable: the template /nix/store/\d+-adk-drc\.in was not kept")

    def test_the_checkouts_carrier_not_kept(self):
        self.fx["new"]["carrier"] = None
        self.refused(r"unreadable: the checkout's bin/adk-verify-carrier-drc was not kept")

    def test_drvs_refuses_what_is_not_a_drv(self):
        write(self.tmp.name, self.fx)
        path = os.path.join(self.tmp.name, "bad.attrs.json")
        with open(path, "w") as handle:
            json.dump({"shells": {"cli": "/nix/store/" + "0" * 32 + "-nix-shell"}}, handle)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tail_eval.main(["drvs", path])
        self.assertEqual(code, 1)


class RealShows(unittest.TestCase):
    """The reader on what Nix 2.30.1 printed for this repository (revs.json
    names the revision). The kept templates are replaced with this revision's
    own, so that what is read is Nix's printing and not a template changed
    since; one test keeps the printed chiplet-export.in, which predates its
    GDS_TO_KICAD_ROOT guard."""

    REAL = os.path.join(HERE, "..", "tests", "nix-tail", "nix-2.30.1")
    TEMPLATES = ("chiplet-export.in", "adk-drc.in", "adk-verify-carrier-drc.in")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.where = os.path.join(self.tmp.name, "kept")
        shutil.copytree(self.REAL, self.where)
        sources = os.path.join(self.where, "new.sources")
        kept = os.listdir(sources)
        for name in self.TEMPLATES:
            (store_name,) = [k for k in kept if k[32:] == "-" + name]
            shutil.copyfile(os.path.join(HERE, name), os.path.join(sources, store_name))

    def printed(self, name):
        """Put back the template Nix's store held when the fixture was printed."""
        sources = os.path.join(self.REAL, "new.sources")
        (store_name,) = [k for k in os.listdir(sources) if k[32:] == "-" + name]
        shutil.copyfile(os.path.join(sources, store_name), os.path.join(self.where, "new.sources", store_name))

    def load(self, name):
        with open(os.path.join(self.where, name)) as handle:
            return json.load(handle)

    def dump(self, name, value):
        with open(os.path.join(self.where, name), "w") as handle:
            json.dump(value, handle)

    def compare(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tail_eval.main(["compare", self.where])
        return code, err.getvalue()

    def roots(self, shell):
        attrs, drvs = self.load("new.attrs.json"), self.load("new.show-ci.json")
        drv = self.load("new.show.json")[attrs["shells"][shell]]
        ci = tail_eval.closure_info(drv, shell)
        return ci, tail_eval.closure_roots(drvs[ci], shell)

    def test_compare_passes(self):
        code, err = self.compare()
        self.assertEqual(code, 0, err)
        self.assertIn("tail = cli + tail-wrappers + tail-export-python + kicad-fork-base + sg13g2-pdk", err)

    def test_the_printed_export_wrapper_is_refused_for_its_missing_guard(self):
        self.printed("chiplet-export.in")
        code, err = self.compare()
        self.assertEqual(code, 1, err)
        self.assertRegex(err, r"tail-wrappers: chiplet-export: command 4 is 'exec .*, "
                              r"not ': \"\$\{GDS_TO_KICAD_ROOT")

    def test_closure_infos_are_in_the_show_form(self):
        for ci in self.load("new.show-ci.json").values():
            self.assertEqual(set(ci["env"]), {"out"})
            self.assertIn("exportReferencesGraph", ci["structuredAttrs"])

    def test_tail_roots_are_the_cli_roots_plus_four(self):
        _, cli = self.roots("cli")
        _, tail = self.roots("tail")
        self.assertEqual(len(cli), 6)
        self.assertEqual(len(tail), 10)
        added = sorted(os.path.basename(p)[33:] for p in set(tail) - set(cli))
        self.assertEqual(added, ["adk-tail-export-python", "adk-tail-wrappers", "ihp-open-pdk-22f4335",
                                 "kicad-base-5d12a25667"])

    def test_closure_infos_lists_the_three_shells(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tail_eval.main(["closure-infos", os.path.join(self.where, "new.show.json")])
        self.assertEqual(code, 0)
        self.assertEqual(set(out.getvalue().split()), set(self.load("new.show-ci.json")))

    def test_tail_root_dropped_refuses(self):
        ci, _ = self.roots("tail")
        show = self.load("new.show-ci.json")
        show[ci]["structuredAttrs"]["exportReferencesGraph"]["closure"].pop()
        self.dump("new.show-ci.json", show)
        code, err = self.compare()
        self.assertEqual(code, 1, err)
        self.assertIn("tail: closure roots are not the cli shell's plus", err)

    def test_cli_changed_refuses(self):
        attrs, show = self.load("new.attrs.json"), self.load("new.show.json")
        show[attrs["shells"]["cli"]]["env"]["ADK_NIX_ROOTS_EXTRA"] = "x"
        self.dump("new.show.json", show)
        code, err = self.compare()
        self.assertEqual(code, 1, err)
        self.assertIn("cli: differs from the base in env.ADK_NIX_ROOTS_EXTRA", err)

    def test_wrapper_sources_are_the_kept_files(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tail_eval.main(["wrapper-sources", os.path.join(self.where, "new.attrs.json"),
                                   os.path.join(self.where, "new.show.json")])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.path.basename(p) for p in out.getvalue().split()),
                         sorted(os.listdir(os.path.join(self.where, "new.sources"))))

    def wrappers(self):
        attrs, show = self.load("new.attrs.json"), self.load("new.show.json")
        return show, show[attrs["packages"]["tail-wrappers"]]

    def refuses(self, pattern):
        code, err = self.compare()
        self.assertEqual(code, 1, err)
        self.assertRegex(err, pattern)

    def test_an_override_that_strips_p_refuses(self):
        # The build command of the flake test-check-tail-wrappers.sh passes:
        # packages.tail-wrappers bound, quoted or split over lines, to
        # tail-wrappers.overrideAttrs (o: { buildCommand = o.buildCommand
        # + "<tests/nix-tail/strip-p-command>"; }).
        show, drv = self.wrappers()
        drv["env"]["buildCommand"] += STRIP_P
        self.dump("new.show.json", show)
        self.refuses("tail-wrappers: the build command's line 20 is " + re.escape(repr(STRIP_P)) + ", not ''")

    def test_a_rebinding_to_other_templates_refuses(self):
        # "tail-wrappers" = ... substituting a copy of the templates without -p.
        show, drv = self.wrappers()
        old = next(p for p in drv["inputSrcs"] if p.endswith("-chiplet-export.in"))
        new = "/nix/store/" + "0" * 32 + "-chiplet-export.in"
        drv["env"]["buildCommand"] = drv["env"]["buildCommand"].replace(old, new)
        drv["inputSrcs"] = sorted(set(drv["inputSrcs"]) - {old} | {new})
        self.dump("new.show.json", show)
        with open(os.path.join(self.where, "new.sources", old[len("/nix/store/"):]), "rb") as handle:
            text = handle.read()
        with open(os.path.join(self.where, "new.sources", new[len("/nix/store/"):]), "wb") as handle:
            handle.write(text.replace(b"#!@bash@ -p\n", b"#!@bash@\n"))
        self.refuses(r"tail-wrappers: chiplet-export: starts '#!/nix/store/35yc81pz0q5yba14lxhn5r3jx5yg6c3l-"
                     r"bash-interactive-5\.3p3/bin/bash', not")

    def test_a_rebinding_to_another_python_refuses(self):
        show, drv = self.wrappers()
        export = next(p for p in tail_eval.STORE_WORD.findall(drv["env"]["buildCommand"])
                      if p.endswith("-adk-tail-export-python"))
        python = "/nix/store/jj6jldlw37r8yy9kc1smrax9dhnjm2x4-python3-3.13.9"
        drv["env"]["buildCommand"] = drv["env"]["buildCommand"].replace(export, python)
        self.dump("new.show.json", show)
        self.refuses(r"tail-wrappers: fills @exportPython@ with " + python + ", not tail-export-python")

    def test_another_carrier_script_refuses(self):
        with open(os.path.join(self.where, "new.carrier-drc"), "ab") as handle:
            handle.write(b"# changed\n")
        self.refuses(r"tail-wrappers: /nix/store/5aivil7rglb7g4vc8brwdjc0ymqr3cz4-adk-verify-carrier-drc is not "
                     r"the checkout's bin/adk-verify-carrier-drc")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--write-fixture"]:
        where, base_rev, new_rev = sys.argv[2:5]
        write(where, fixture(base_rev, new_rev), os.path.join(where, "store"))
        sys.exit(0)
    if sys.argv[1:2] == ["--write-carrier"]:
        with open(sys.argv[2], "wb") as handle:
            handle.write(CARRIER)
        sys.exit(0)
    unittest.main()
