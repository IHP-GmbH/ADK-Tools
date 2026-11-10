#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""A checkout can stand in for one staged tree of an image without a reinstall.

run.sh's ADK_STAGING_OVERLAY mounts a host checkout read-only over
/opt/adk-tools/staging/<name>. The venv's editable install of that tree then
imports the checkout's code, but its metadata stays the image's: the console
scripts pip wrote and the top-level packages it recorded. So the checkout has
to declare the same ones, or the run would start scripts that are not there,
or miss packages the image never put on the path. A tree that differs needs a
reinstall, which is an image build.

The check runs in a throwaway container of the image (no network, read-only
root, 512m, 1 CPU, the caller's uid) with the checkout mounted where run.sh
mounts it. In there it finds each editable distribution of the venv whose
project lies in the staged tree (a tree may hold more than one project), reads
its entry_points.txt and top_level.txt, and asks setuptools (the image's build
backend, from its wheelhouse) for the same two files from the checkout's
project, written outside the checkout.
setuptools runs there without the venv's site-packages, as pip's isolated
build ran it when the image installed the tree, with nothing but the
wheelhouse: the venv's own packaging is older than the one setuptools vendors,
and a project with an SPDX license string needs the newer one. An image with
no wheelhouse (one built before it) gets egg_info from the venv's own
setuptools and packages instead, so it refuses such a project. A project in
one of the folders PROJECTS lists for the tree, where the image installs
none, is refused too: its commands would not be there; no other folder of the
checkout is looked at. Any difference is refused. This script is
mounted from the caller's checkout, so it also checks images built before it.

Usage:
    check_overlay.py --image REF --name NAME --path DIR
    check_overlay.py --self-test
Exit 0 when the checkout matches, 1 when it does not or the check cannot run,
2 on usage. On 1 the reason is the last line on stderr; the lines before it,
if any, are the check container's stderr, which holds the whole stderr of the
step in there that refused.
"""
import argparse
import configparser
import contextlib
import glob
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlparse

# Each staged tree's projects the image installs editable, as folders of the
# tree; the Dockerfile's `pip install -e` and `COPY staging/` lines and run.sh's
# STAGED_TREES name the same ones (the self-test holds them together). Only
# these folders are looked at for a project the image does not install.
PROJECTS = {
    "assembly-lvs": (".",),
    "chiplet-cosim": (".",),
    "chiplet-flow": (".",),
    "chiplet-spec": ("reference/python", "description"),
    "interposer-pnr": (".",),
    "packaging-flow": (".",),
}
STAGED_TREES = tuple(PROJECTS)
STAGING = Path("/opt/adk-tools/staging")
VENV_PYTHON = "/opt/adk-tools/venv/bin/python3"
WHEELS = Path("/opt/adk-tools/tools/wheels")
MOUNTED_AS = "/run/adk-check-overlay.py"
CONTAINER = ("--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
             "--memory", "512m", "--memory-swap", "512m", "--cpus", "1")
TIMEOUT_S = 300


class Refused(Exception):
    """The one reason the checkout cannot stand in for the staged tree, and
    the whole stderr of the step that gave it, when one did."""

    def __init__(self, why, detail=""):
        super().__init__(why)
        self.detail = detail


# ----------------------------------------------------------------------
# in the container
# ----------------------------------------------------------------------
def entry_points(text):
    """{group: {name: target}} of an entry_points.txt."""
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.optionxform = str
    parser.read_string(text)
    return {group: {name: " ".join(value.split()) for name, value in parser[group].items()}
            for group in parser.sections()}


def read_meta(folder):
    """(entry points, top-level packages) a dist-info or egg-info folder records."""
    folder = Path(folder)
    eps = folder / "entry_points.txt"
    top = folder / "top_level.txt"
    if not top.is_file():
        raise Refused(f"{folder} has no top_level.txt")
    return (entry_points(eps.read_text()) if eps.is_file() else {},
            sorted(line.strip() for line in top.read_text().splitlines() if line.strip()))


def installed(site_dirs, staged):
    """[(dist-info folder, project folder)] of each editable distribution
    whose project lies in the staged tree, at least one."""
    found = []
    for site in site_dirs:
        for dist in sorted(glob.glob(os.path.join(site, "*.dist-info"))):
            try:
                url = json.loads(Path(dist, "direct_url.json").read_text())
            except (OSError, ValueError):
                continue
            if not (url.get("dir_info") or {}).get("editable"):
                continue
            parsed = urlparse(url.get("url", ""))
            project = Path(unquote(parsed.path))
            if parsed.scheme == "file" and (project == staged or staged in project.parents):
                found.append((Path(dist), project))
    if not found:
        raise Refused(f"the image has no editable install in {staged}")
    return found


def declared(project, work, python=sys.executable, wheelhouse=WHEELS):
    """The egg-info setuptools writes for the project into work, the project
    itself untouched (it is mounted read-only)."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    flags = ["-B"]
    wheels = sorted(wheelhouse.glob("setuptools-*.whl")) if wheelhouse.is_dir() else []
    if wheels:
        target = Path(work, "setuptools")
        got = subprocess.run([python, "-m", "pip", "install", "-q", "--no-cache-dir", "--no-index",
                              "--disable-pip-version-check", "--target", str(target), str(wheels[-1])],
                             capture_output=True, text=True)
        if got.returncode != 0:
            raise Refused(f"the wheelhouse's setuptools did not install: {last_line(got.stderr)}",
                          got.stderr)
        env["PYTHONPATH"] = str(target)
        # -S: the venv's site-packages stays off the path, so this setuptools
        # imports the packages it vendors and not the venv's older ones.
        flags.append("-S")
    base = Path(work, "egg-base")
    base.mkdir()
    argv = ([python, *flags, "setup.py"] if Path(project, "setup.py").is_file()
            else [python, *flags, "-c", "import setuptools; setuptools.setup()"])
    got = subprocess.run(argv + ["-q", "egg_info", "--egg-base", str(base)], cwd=project,
                         env=env, capture_output=True, text=True)
    infos = sorted(base.glob("*.egg-info"))
    if got.returncode != 0 or len(infos) != 1:
        raise Refused(f"setuptools could not read the checkout's project: {last_line(got.stderr)}",
                      got.stderr)
    return infos[0]


def compare_metadata(image, overlay):
    """The first difference between the image's metadata and the overlay's."""
    (image_eps, image_top), (overlay_eps, overlay_top) = image, overlay
    if image_top != overlay_top:
        raise Refused(f"top-level packages {overlay_top}, the image's {image_top}")
    for group in sorted(set(image_eps) | set(overlay_eps)):
        mine, theirs = overlay_eps.get(group, {}), image_eps.get(group, {})
        if mine != theirs:
            names = sorted(set(mine) ^ set(theirs)) or sorted(
                n for n in mine if mine[n] != theirs.get(n))
            raise Refused(f"entry points [{group}] differ from the image's at {', '.join(names)}")


def check_tree(site_dirs, staged, projects, declare=declared):
    """The names of the dist-info folders checked, each one's metadata equal
    to what its project in the checkout declares. Refused when the checkout
    holds a project in one of the tree's PROJECTS folders (only those are
    looked at) where the image installs none, and, prefixed with an
    install's dist-info name, when that install's check refuses."""
    found = installed(site_dirs, staged)
    for folder in projects:
        where = (staged / folder).resolve()
        if (any((where / f).is_file() for f in ("pyproject.toml", "setup.py"))
                and where not in [project.resolve() for _, project in found]):
            at = "the tree's root" if folder == "." else folder
            raise Refused(f"the checkout has a project at {at} that this image does not "
                          "install: its commands would be missing; that needs an image build")
    names = []
    for dist, project in found:
        if not project.is_dir():
            raise Refused(f"{dist.name}: the checkout has no {project.relative_to(staged)}, "
                          "where the image installs a project")
        with tempfile.TemporaryDirectory() as work:
            try:
                compare_metadata(read_meta(dist), read_meta(declare(project, work)))
            except Refused as exc:
                raise Refused(f"{dist.name}: {exc}", exc.detail) from None
        names.append(dist.name)
    return names


def inner(name, check=check_tree):
    import site
    names = check(site.getsitepackages(), STAGING / name, PROJECTS[name])
    return f"{name}: entry points and top-level packages equal the image's ({', '.join(names)})"


# ----------------------------------------------------------------------
# on the host
# ----------------------------------------------------------------------
def last_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def docker_argv(image, name, path):
    return ["docker", "run", *CONTAINER, "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-v", f"{Path(__file__).resolve()}:{MOUNTED_AS}:ro",
            "-v", f"{path}:{STAGING / name}:ro", "--entrypoint", VENV_PYTHON,
            image, "-B", "-I", MOUNTED_AS, "--inner", name]


def outer(image, name, path):
    """None when the checkout matches; Refused, with the container's stderr, when not."""
    if name not in STAGED_TREES:
        raise Refused(f"{name!r} is not a staged tree ({', '.join(STAGED_TREES)})")
    if not os.path.isdir(path):
        raise Refused(f"{path} is not a directory")
    try:
        got = subprocess.run(docker_argv(image, name, os.path.realpath(path)),
                             capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"the check container did not run ({type(exc).__name__})") from None
    if got.returncode == 1 and got.stdout.strip():
        raise Refused(last_line(got.stdout), got.stderr)
    if got.returncode != 0:
        raise Refused(f"the check container did not run (docker exit {got.returncode}: "
                      f"{last_line(got.stderr)})", got.stderr)


# ----------------------------------------------------------------------
# self-test
# ----------------------------------------------------------------------
def self_test():
    cases = 0

    def refused(call):
        try:
            call()
        except Refused as exc:
            return str(exc)
        return None

    def refused_with(call):
        try:
            call()
        except Refused as exc:
            return str(exc), exc.detail
        return None

    dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
    installs = {}
    text = "\n".join(line for line in dockerfile.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    for path in re.findall(r'(?:^|\s)(?:-e|--editable)[\s=]+["\']?/opt/adk-tools/staging/'
                           r'([^"\'\s\[]+)', text, re.M):
        tree, _, folder = path.partition("/")
        installs.setdefault(tree, set()).add(folder or ".")
    assert installs == {tree: set(folders) for tree, folders in PROJECTS.items()}, installs
    copied = set(re.findall(r"^COPY(?:\s+--\S+)*\s+staging/([^/\s]+)\s+/opt/adk-tools/staging/",
                            text, re.M))
    assert copied == set(PROJECTS), copied
    run_sh = Path(__file__).resolve().parent.parent / "run.sh"
    trees = re.findall(r'^STAGED_TREES="([^"]*)"', run_sh.read_text(), re.M)
    assert len(trees) == 1 and sorted(trees[0].split()) == sorted(PROJECTS), trees
    cases += 1

    eps = entry_points("[console_scripts]\nflow = a.cli:main\n\n[x.plugins]\nB =  b:mod\n")
    assert eps == {"console_scripts": {"flow": "a.cli:main"}, "x.plugins": {"B": "b:mod"}}, eps
    cases += 1
    base = ({"console_scripts": {"flow": "a.cli:main"}}, ["a"])
    same = ({"console_scripts": {"flow": "a.cli:main"}}, ["a"])
    assert refused(lambda: compare_metadata(base, same)) is None
    cases += 1
    why = refused(lambda: compare_metadata(base, ({"console_scripts": {"flow": "a.cli:main",
                                                                       "new": "a.new:main"}},
                                                   ["a"])))
    assert why == "entry points [console_scripts] differ from the image's at new", why
    cases += 1
    other = ({"console_scripts": {"flow": "a.cli:other"}}, ["a"])
    why = refused(lambda: compare_metadata(base, other))
    assert why == "entry points [console_scripts] differ from the image's at flow", why
    cases += 1
    why = refused(lambda: compare_metadata(base, (base[0], ["a", "b"])))
    assert why == "top-level packages ['a', 'b'], the image's ['a']", why
    cases += 1

    with tempfile.TemporaryDirectory() as tmp:
        site, staged = Path(tmp, "site"), Path(tmp, "staging", "chiplet-spec")
        site.mkdir(parents=True)

        def dist(folder, url, editable=True, top="pkg\n"):
            d = site / folder
            d.mkdir()
            (d / "direct_url.json").write_text(json.dumps(
                {"url": url, "dir_info": {"editable": editable}}))
            (d / "top_level.txt").write_text(top)
            return d

        dist("other-1.dist-info", f"file://{Path(tmp, 'staging', 'interposer-pnr')}")
        dist("frozen-1.dist-info", f"file://{staged}", editable=False)
        assert refused(lambda: installed([str(site)], staged)) == \
            f"the image has no editable install in {staged}"
        cases += 1
        want = dist("chiplet_format_io-1.dist-info", f"file://{staged}/reference/python")
        assert installed([str(site)], staged) == [(want, staged / "reference" / "python")]
        cases += 1
        desc = dist("desc-1.dist-info", f"file://{staged}/description", top="desc\n")
        (desc / "entry_points.txt").write_text("[console_scripts]\ndesc = desc.cli:main\n")
        assert installed([str(site)], staged) == [
            (want, staged / "reference" / "python"), (desc, staged / "description")]
        cases += 1

        def declares(said):
            """A stand-in for declared(): the egg-info each project's checkout says."""
            def declare(project, work):
                if said[project] is None:
                    raise Refused("no", "trace\n")
                eps, top = said[project]
                info = Path(work, "x.egg-info")
                info.mkdir()
                (info / "top_level.txt").write_text(top)
                if eps:
                    (info / "entry_points.txt").write_text(eps)
                return info
            return declare

        def lacks(at):
            return (f"the checkout has a project at {at} that this image does not install: "
                    "its commands would be missing; that needs an image build")

        reader, description = staged / "reference" / "python", staged / "description"
        reader.mkdir(parents=True)
        description.mkdir()
        same = {reader: ("", "pkg\n"),
                description: ("[console_scripts]\ndesc = desc.cli:main\n", "desc\n")}
        assert check_tree([str(site)], staged, (), declares(same)) == [want.name, desc.name]
        cases += 1
        extra = staged / "extra"
        extra.mkdir()
        listed = ("reference/python", "extra", "description")
        for folder in (reader, description):
            (folder / "pyproject.toml").write_text("")
        assert check_tree([str(site)], staged, listed, declares(same)) == [want.name, desc.name]
        cases += 1
        (extra / "pyproject.toml").write_text("")
        assert refused(lambda: check_tree([str(site)], staged, listed, declares(same))) == \
            lacks("extra")
        cases += 1
        for folder in (reader, description, extra):
            (folder / "pyproject.toml").unlink()
        root = Path(tmp, "staging", "chiplet-flow")
        root.mkdir()
        (root / "setup.py").write_text("")
        flow = dist("flow-1.dist-info", f"file://{root}")
        assert check_tree([str(site)], root, (".",), declares({root: ("", "pkg\n")})) == \
            [flow.name]
        cases += 1
        bare = Path(tmp, "staging", "chiplet-cosim")
        (bare / "sub").mkdir(parents=True)
        (bare / "setup.py").write_text("")
        dist("cosim-1.dist-info", f"file://{bare}/sub")
        assert refused(lambda: check_tree([str(site)], bare, (".",),
                                          declares({bare / "sub": ("", "pkg\n")}))) == \
            lacks("the tree's root")
        cases += 1
        moved = dict(same)
        moved[description] = ("[console_scripts]\ndesc = desc.cli:other\n", "desc\n")
        assert refused(lambda: check_tree([str(site)], staged, (), declares(moved))) == \
            "desc-1.dist-info: entry points [console_scripts] differ from the image's at desc"
        cases += 1
        failed = dict(same)
        failed[description] = None
        assert refused_with(lambda: check_tree([str(site)], staged, (), declares(failed))) == \
            ("desc-1.dist-info: no", "trace\n")
        cases += 1
        description.rmdir()
        assert refused(lambda: check_tree([str(site)], staged, (), declares(same))) == \
            "desc-1.dist-info: the checkout has no description, where the image installs a project"
        cases += 1
        assert read_meta(want) == ({}, ["pkg"])
        cases += 1
        (want / "top_level.txt").unlink()
        assert refused(lambda: read_meta(want)) == f"{want} has no top_level.txt"
        cases += 1

        # declared() with a python that records how it was run.
        project, wheelhouse, log = Path(tmp, "proj"), Path(tmp, "wheels"), Path(tmp, "py.log")
        project.mkdir()
        wheelhouse.mkdir()
        python = Path(tmp, "python")
        python.write_text('#!/bin/sh\n'
                          f'printf "%s|PYTHONPATH=%s\\n" "$*" "${{PYTHONPATH-}}" >> \'{log}\'\n'
                          'for a; do last="$a"; done\n'
                          'case "$*" in *egg_info*) mkdir -p "$last/x.egg-info" ;; esac\n')
        python.chmod(0o755)
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/sentinel"}):
            with tempfile.TemporaryDirectory() as work:
                declared(project, work, str(python), wheelhouse)
                runs = log.read_text().splitlines()
                assert len(runs) == 1 and runs[0].startswith("-B -c import setuptools"), runs
                assert runs[0].endswith("|PYTHONPATH=/sentinel"), runs
            cases += 1
            log.unlink()
            Path(wheelhouse, "setuptools-84.0.0-py3-none-any.whl").write_text("")
            with tempfile.TemporaryDirectory() as work:
                info = declared(project, work, str(python), wheelhouse)
                pip, egg_info = log.read_text().splitlines()
                assert pip.startswith("-m pip install") and pip.endswith(
                    "-any.whl|PYTHONPATH=/sentinel"), pip
                assert egg_info == (
                    "-B -S -c import setuptools; setuptools.setup() -q egg_info "
                    f"--egg-base {work}/egg-base|PYTHONPATH={work}/setuptools"), egg_info
                assert info == Path(work, "egg-base", "x.egg-info")
            cases += 1

        # --inner: the refusing step's stderr on stderr, the reason on stdout.
        def refuse(site_dirs, staged, projects):
            raise Refused("x-1.dist-info: why", "trace\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["--inner", "chiplet-spec"], check=refuse)
        said = (rc, out.getvalue(), err.getvalue())
        assert said == (1, "chiplet-spec: x-1.dist-info: why\n", "trace\n"), said
        cases += 1

        stub = Path(tmp, "bin")
        stub.mkdir()
        (stub / "docker").write_text("#!/bin/sh\n[ -z \"$STUB_SAY\" ] || echo \"$STUB_SAY\"\n"
                                     "echo \"docker said no\" >&2\nexit \"${STUB_RC:-0}\"\n")
        (stub / "docker").chmod(0o755)
        path = os.environ["PATH"]
        try:
            os.environ["PATH"] = f"{stub}{os.pathsep}{path}"
            assert outer("sha256:" + "0" * 64, "chiplet-spec", tmp) is None
            cases += 1
            os.environ.update(STUB_RC="1",
                              STUB_SAY="chiplet-spec: x-1.dist-info: top-level packages differ")
            assert refused_with(lambda: outer("i", "chiplet-spec", tmp)) == \
                ("chiplet-spec: x-1.dist-info: top-level packages differ", "docker said no\n")
            cases += 1
            os.environ.update(STUB_RC="125", STUB_SAY="")
            assert refused_with(lambda: outer("i", "chiplet-spec", tmp)) == \
                ("the check container did not run (docker exit 125: docker said no)",
                 "docker said no\n")
            cases += 1
            assert refused(lambda: outer("i", "adk-tools", tmp)).startswith(
                "'adk-tools' is not a staged tree")
            cases += 1
            assert refused(lambda: outer("i", "chiplet-spec", Path(tmp, "absent"))).endswith(
                "is not a directory")
            cases += 1
        finally:
            os.environ["PATH"] = path
            for key in ("STUB_RC", "STUB_SAY"):
                os.environ.pop(key, None)
        argv = docker_argv("img", "packaging-flow", "/src/pf")
        assert argv[argv.index("img") - 2:argv.index("img")] == ["--entrypoint", VENV_PYTHON]
        assert "/src/pf:/opt/adk-tools/staging/packaging-flow:ro" in argv
        assert argv[-2:] == ["--inner", "packaging-flow"] and "--network" in argv
        cases += 1
    print(f"check_overlay self-test: {cases} cases OK")
    return 0


def main(argv=None, check=check_tree):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--inner", metavar="NAME", help=argparse.SUPPRESS)
    parser.add_argument("--image")
    parser.add_argument("--name")
    parser.add_argument("--path")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.inner:
        try:
            print(inner(args.inner, check))
        except Refused as exc:
            sys.stderr.write(exc.detail)
            print(f"{args.inner}: {exc}")
            return 1
        return 0
    if not (args.image and args.name and args.path):
        parser.print_usage(sys.stderr)
        return 2
    try:
        outer(args.image, args.name, args.path)
    except Refused as exc:
        why = str(exc)
        if exc.detail.strip():
            print(exc.detail.rstrip("\n"), file=sys.stderr)
    else:
        return 0
    print(f"check_overlay: {args.name}: {why}" if not why.startswith(f"{args.name}: ")
          else f"check_overlay: {why}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
