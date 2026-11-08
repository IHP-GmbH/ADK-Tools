#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Every path the image build reaches for must exist in the pinned tree.

adk-tools is a lockfile: it pins seven repositories as submodules and the
Dockerfile addresses their contents by path. A pin bump can therefore move a
file out from under the Dockerfile without changing a single line of this
repository, and nothing here notices until the image build reaches that step,
which is roughly an hour in.

That is not hypothetical. When the plugin reorganised itself into
`plugins/<name>/`, five references were left addressing the old flat layout,
two of them in the runtime image rather than the build, so they were broken for
users rather than for us. The build eventually said so. It said so an hour
late, and only because somebody ran it.

This check answers the same question in about a second, with no Docker and no
build. It is deliberately literal: it resolves the paths, it does not run the
steps. What it proves is that the pinned tree still has the shape the Dockerfile
assumes, which is exactly the property a pin bump breaks.

Two resolutions are worth naming because a plain path check misses both, and
both are real defects this repository has shipped:

  - a `cd` inside a RUN, after which `pytest tests` means something relative to
    somewhere else. The path that broke was the one after the `cd`.
  - the KiCad plugin symlinks. Their target existed perfectly well after the
    reorganisation; it just stopped being a plugin package, so KiCad scanned it,
    found no `__init__.py`, and registered nothing. Existence was never the
    question there.

Usage:
    check_paths.py
    check_paths.py --self-test
"""

import argparse
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile

IMAGE_ROOTS = ("/opt/adk-tools/", "/usr/local/bin/")
KICAD_PLUGIN_DIR = "/usr/share/kicad/scripting/plugins/"

# Image paths that no COPY produces because a RUN makes them. Each one is a
# deliberate exemption with the reason it is exempt, not a silent skip: a path
# that matches none of these and no COPY is a failure, so the list cannot rot
# into a catch-all without somebody noticing.
BUILT_AT_RUNTIME = {
    "/opt/adk-tools/manifest.json": "written by the MANIFEST_B64 RUN",
    "/opt/adk-tools/.demo-tracked-snapshot": "copied by verify step 4a",
}
# Matched exactly rather than as a subtree. `/opt/adk-tools` as a prefix
# exemption would swallow every unmapped path under it and turn this checker
# into the catch-all the list above exists not to be.
BUILT_EXACT = {
    "/opt/adk-tools": "the image prefix itself (COPY LICENSE NOTICE.md dest)",
    "/opt/adk-tools/staging":
        "the parent of the staged trees, each COPY'd as staging/<name>; verify "
        "step 0b walks it whole",
    # The venv used to sit in BUILT_AT_RUNTIME, where it was a PREFIX, so
    # `/opt/adk-tools/venv/bin/<anything>` was exempt with the reason "created
    # by the `python3 -m venv` RUN". That reason was true for python3 and pip
    # and false for everything else, and once console scripts started landing
    # there a wrapper could name a script no install creates and still pass.
    # Listed one by one instead, each with the step that actually makes it.
    "/opt/adk-tools/venv": "created by the `python3 -m venv` RUN",
    "/opt/adk-tools/venv/bin/python3": "the venv's own interpreter",
    "/opt/adk-tools/venv/bin/pip": "the venv's own pip",
    "/opt/adk-tools/venv/bin/interposer-pnr":
        "console script from the editable install of staging/interposer-pnr",
    "/opt/adk-tools/venv/bin/assembly-lvs":
        "console script from the editable install of staging/assembly-lvs",
    "/opt/adk-tools/venv/bin/chiplet-die-flow":
        "console script from the editable install of staging/chiplet-flow",
    "/opt/adk-tools/librelane-launcher/librelane":
        "the LibreLane launcher build.sh writes into the build context (with "
        "nix/build-env.sh) before the image build; verify leg 19 holds it",
    "/opt/adk-tools/nix":
        "the directory the tool identity, host closure and build-env.sh COPYs "
        "fill, and in the verify stage the COPY that carries nix/cli-env.sh; "
        "leg 19 sources both there",
    "/opt/adk-tools/venv/bin/packaging-flow":
        "console script from the editable install of staging/packaging-flow",
    "/opt/adk-tools/venv/bin/chiplet-system-description":
        "console script from the editable install of staging/chiplet-spec/description",
    # COSIM_DEVSHELL: the RUN after the engine's ADD links it to the
    # devshell's store path, which the unpacked Nix closure holds.
    "/opt/adk-tools/cosim-devshell":
        "the co-simulation engine RUN's link to the devshell's store path",
    "/opt/adk-tools/cosim-devshell/bin/python3":
        "the devshell store path's link into the co-simulation env's interpreter",
    # MIXSIM_DEVSHELL: the RUN after the mixed-signal engine's ADD makes the
    # directory and links each part into the unpacked Nix closure.
    "/opt/adk-tools/mixsim-devshell":
        "the mixed-signal engine RUN's mkdir",
    "/opt/adk-tools/mixsim-devshell/bin":
        "the mixed-signal engine RUN's mkdir",
    "/opt/adk-tools/mixsim-devshell/lib":
        "the mixed-signal engine RUN's mkdir",
    "/opt/adk-tools/mixsim-devshell/bin/ngspice":
        "the mixed-signal engine RUN's link to ngspice's bin/ngspice",
    "/opt/adk-tools/mixsim-devshell/bin/iverilog":
        "the mixed-signal engine RUN's link to the libvvp Icarus's bin/iverilog",
    "/opt/adk-tools/mixsim-devshell/bin/vvp":
        "the mixed-signal engine RUN's link to the libvvp Icarus's bin/vvp",
    "/opt/adk-tools/mixsim-devshell/lib/libvvp.so":
        "the mixed-signal engine RUN's link to the libvvp Icarus's lib/libvvp.so",
    "/opt/adk-tools/mixsim-devshell/lib/ngspice":
        "the mixed-signal engine RUN's link to ngspice's lib/ngspice",
    "/opt/adk-tools/mixsim-devshell/osdi":
        "the mixed-signal engine RUN's link to the OSDI models' osdi",
    # The four OSDI files the sg13g2 models RUN copies into the PDK slice, by
    # name, from the store path that link points at; nix/sg13g2-osdi.sha256
    # names each, and that RUN checks the copies against it.
    "/opt/adk-tools/mixsim-devshell/osdi/psp103.osdi":
        "the mixed-signal engine RUN's link to the OSDI models' osdi, which holds it",
    "/opt/adk-tools/mixsim-devshell/osdi/psp103_nqs.osdi":
        "the mixed-signal engine RUN's link to the OSDI models' osdi, which holds it",
    "/opt/adk-tools/mixsim-devshell/osdi/r3_cmc.osdi":
        "the mixed-signal engine RUN's link to the OSDI models' osdi, which holds it",
    "/opt/adk-tools/mixsim-devshell/osdi/mosvar.osdi":
        "the mixed-signal engine RUN's link to the OSDI models' osdi, which holds it",
    # VAMS_DEVSHELL: the RUN after the Verilog-AMS engine's ADD links it to
    # the devshell's store path, which the unpacked Nix closure holds.
    "/opt/adk-tools/vams-devshell":
        "the Verilog-AMS engine RUN's link to the devshell's store path",
    # The viewers: the RUN after their ADD links each onto PATH, into the
    # unpacked Nix closure.
    "/usr/local/bin/gtkwave":
        "the viewers RUN's link to GTKWave's bin/gtkwave",
    "/usr/local/bin/gaw":
        "the viewers RUN's link to GAW's bin/gaw",
    "/usr/local/bin/xschem":
        "the viewers RUN's link to Xschem's bin/xschem",
    "/opt/adk-tools/xschem":
        "the viewers RUN's link to Xschem's store path",
    "/opt/adk-tools/xschem/share/xschem":
        "share/xschem in Xschem's store path, through the viewers RUN's link",
    # The tools stage's own folder: the build backend's wheelhouse, which the
    # staged trees' offline install finds below the thin-layer line, and the
    # two locks naming every package the tools image holds.
    "/opt/adk-tools/tools": "made by the tools stage's wheelhouse and lock RUNs",
    "/opt/adk-tools/tools/wheels":
        "the wheelhouse the tools stage's setuptools RUN downloads into",
    "/opt/adk-tools/tools/apt.lock": "written by the tools stage's lock RUN (dpkg-query)",
    "/opt/adk-tools/tools/pip.lock": "written by the tools stage's lock RUN (pip freeze)",
    "/opt/adk-tools/tool-identity.json":
        "the tool identity table nix/tool-identity-image.sh writes in the runtime stage",
    # Die views: COPY'd from die-views/, which stage-die-views.sh writes
    # into the build context from a die run's records and which git never
    # holds, so CI has no tree to resolve it against. die-views.sha256, which
    # pins it, is tracked and is checked as a path like any other.
    "/opt/adk-tools/die-views":
        "COPY'd from die-views/, staged by stage-die-views.sh and checked "
        "against die-views.sha256 by the RUN after it",
}

# `*` is inside the character class on purpose: a glob has to arrive here whole
# so it can be reported as unchecked. Stripped off silently it becomes a plain
# directory that resolves, and the run then claims coverage it does not have.
TOKEN = re.compile(r"[A-Za-z0-9_./\-\$\{\}\*]*(?:/opt/adk-tools|/usr/local/bin)[A-Za-z0-9_./\-\$\{\}\*]*")
FOR_LOOP = re.compile(r"\bfor\s+(\w+)\s+in\s+([^;\n]+?)\s*;\s*do\b")


def declared_staged(root):
    """Tree names stage-sources.sh writes under staging/, as a closed set: the
    stage names ci/pin-set.json gives its staged repositories.

    staging/ is not in git: it is produced from staging.pin before a build and
    is absent in a fresh clone, which is where CI runs. So the paths reaching
    into it cannot simply be resolved against the working tree the way a
    submodule path is. They also cannot simply be waved through, because a
    reference into a staged tree goes stale on a pin bump for exactly the same
    reason a submodule path does, which is the whole subject of this file.

    The set makes the difference checkable. A name this file declares and that
    is not on disk is reported as unstaged, by name, and skipped. A name it does
    not declare is a failure wherever it appears, staging present or not, so a
    typo cannot hide behind the same absence.
    """
    pin_set = pathlib.Path(root) / "ci" / "pin-set.json"
    if not pin_set.is_file():
        return None
    repos = json.loads(pin_set.read_text())["repos"]
    return {e["stage_name"] for e in repos.values() if e["stage_name"]}


def instructions(dockerfile_text):
    """Yield (keyword, body) with line continuations joined."""
    joined, buf = [], ""
    for raw in dockerfile_text.splitlines():
        line = raw.rstrip()
        if buf:
            buf += "\n" + line
        elif line.strip().startswith("#") or not line.strip():
            continue
        else:
            buf = line
        if buf.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1]
            continue
        joined.append(buf)
        buf = ""
    if buf:
        joined.append(buf)

    for stmt in joined:
        parts = stmt.split(None, 1)
        if len(parts) == 2:
            yield parts[0].upper(), parts[1]


def copy_map(dockerfile_text):
    """(source COPYs, artifact prefixes, multi-source COPYs).

    {image prefix: repo path}, {prefix: stage} and [(image path, repo path)]
    for each source of a COPY that names more than one.
    """
    sources, artifacts, multi = {}, {}, []
    for keyword, body in instructions(dockerfile_text):
        if keyword != "COPY":
            continue
        args = shlex.split(" ".join(body.split()))
        # Every leading flag goes, not only --from: a `COPY --chmod=0444 SRC DST`
        # read with its flag as the source mapped nothing, and every path below
        # DST then failed as matching no COPY.
        from_stage = None
        while args and args[0].startswith("--"):
            flag = args.pop(0)
            if flag.startswith("--from="):
                from_stage = flag[len("--from="):]
        if len(args) < 2:
            continue
        *srcs, dst = args
        if not dst.startswith(IMAGE_ROOTS) and dst.rstrip("/") + "/" not in IMAGE_ROOTS:
            continue
        dst = dst.rstrip("/") or "/"
        if from_stage is not None:
            artifacts[dst] = from_stage
        elif len(srcs) == 1:
            sources[dst] = srcs[0].rstrip("/")
        else:
            # A multi-source COPY (LICENSE NOTICE.md -> dir) puts each file
            # source at dst/<its name>, so each maps on its own. The directory
            # itself is not mapped: as a prefix it would cover every path
            # below it. A directory source would copy its contents, not
            # itself, so check() refuses one rather than map it wrongly.
            for src in srcs:
                src = src.rstrip("/")
                image_path = "%s/%s" % (dst.rstrip("/"), src.rsplit("/", 1)[-1])
                sources[image_path] = src
                multi.append((image_path, src))
    return sources, artifacts, multi


def expand_loops(run_body):
    """Yield the RUN body once per value of any `for X in a b; do` it opens.

    Verify step 6 iterates the plugin packages, so its cwd is a path with a
    shell variable in it. Dropping such a path would silently skip the exact
    step whose paths broke last time.
    """
    m = FOR_LOOP.search(run_body)
    if not m:
        return [run_body]
    var, values = m.group(1), m.group(2).split()
    if any("$" in v for v in values):
        return [run_body]
    out = []
    for value in values:
        body = run_body.replace("${%s}" % var, value)
        body = re.sub(r"\$%s\b" % re.escape(var), value, body)
        out.append(body)
    return out


def literals(run_body):
    """Image paths a RUN names, resolved through any `cd` that precedes them."""
    found = []
    cwd = None
    for chunk in re.split(r"&&|\|\||;|\n", run_body):
        chunk = chunk.strip()
        cd = re.match(r'cd\s+"?([^"\s]+)"?', chunk)
        if cd and (cd.group(1).rstrip("/") + "/").startswith(IMAGE_ROOTS):
            cwd = cd.group(1).rstrip("/")
            # The image root itself is where the rest resolves from, not a
            # path to look up.
            if cwd + "/" not in IMAGE_ROOTS:
                found.append((cwd, "cd target"))
            continue
        for tok in TOKEN.findall(chunk):
            if tok.rstrip("/") + "/" in IMAGE_ROOTS:
                continue
            idx = max(tok.find(root) for root in IMAGE_ROOTS if root in tok)
            found.append((tok[idx:].rstrip("/"), "path"))
        # `pytest <relative>` after a cd: the argument is a path, and the path
        # that broke in the monorepo move was exactly this shape.
        for m in re.finditer(r"pytest\s+((?:-\S+\s+)*)([A-Za-z0-9_][\w./\-]*)", chunk):
            if cwd:
                found.append(("%s/%s" % (cwd, m.group(2)), "pytest target"))
    return found


def symlinked_plugins(run_body):
    """Sources of `ln -s <src> /usr/share/kicad/scripting/plugins/<name>`."""
    out = []
    for m in re.finditer(
        r"ln\s+-s\s+(\S+)\s+(%s\S*)" % re.escape(KICAD_PLUGIN_DIR),
        " ".join(run_body.split()),
    ):
        out.append(m.group(1).rstrip("/"))
    return out


def resolve(image_path, sources, artifacts):
    """('repo', path) | ('artifact', stage) | ('built', reason) | (None, None)."""
    if image_path in BUILT_EXACT:
        return "built", BUILT_EXACT[image_path]
    best = (None, None, -1)
    for prefix, stage in artifacts.items():
        if image_path == prefix or image_path.startswith(prefix + "/"):
            if len(prefix) > best[2]:
                best = ("artifact", stage, len(prefix))
    for prefix, reason in BUILT_AT_RUNTIME.items():
        if image_path == prefix or image_path.startswith(prefix + "/"):
            if len(prefix) > best[2]:
                best = ("built", reason, len(prefix))
    for prefix, repo in sources.items():
        if image_path == prefix or image_path.startswith(prefix + "/"):
            # Strictly longer than an artifact prefix of the same length: the
            # later COPY --from in the final stage is what the image actually
            # holds there.
            if len(prefix) > best[2]:
                rel = image_path[len(prefix):].lstrip("/")
                best = ("repo", "%s/%s" % (repo, rel) if rel else repo, len(prefix))
    return best[0], best[1]


def check(root, dockerfile="Dockerfile"):
    root = pathlib.Path(root)
    text = (root / dockerfile).read_text()
    sources, artifacts, multi = copy_map(text)
    failures, checked, skipped = [], [], []

    for image_path, src in multi:
        if (root / src).is_dir():
            failures.append(
                "%s: COPY names the directory %s beside other sources, so its "
                "contents land in %s, not the directory. Give it a COPY of "
                "its own." % (image_path, src, image_path.rsplit("/", 1)[0])
            )

    if not sources:
        failures.append(
            "%s: no COPY into the image prefix was parsed, so this check would "
            "pass having verified nothing." % dockerfile
        )
        return failures, checked, skipped

    submodule_roots = {v for v in sources.values() if v.startswith("tools/")}
    staged_names = declared_staged(root)

    def audit(image_path, kind, origin):
        if "$" in image_path or "*" in image_path:
            skipped.append((image_path, "%s: unexpanded shell variable or glob" % kind))
            return
        what, where = resolve(image_path, sources, artifacts)
        if what is None:
            failures.append(
                "%s (%s): matches no COPY and no declared build-time path. Either "
                "a new one was introduced, in which case map it, or this is a typo "
                "that the build would only find an hour in." % (image_path, origin)
            )
            return
        if what != "repo":
            skipped.append((image_path, "%s produced by %s" % (kind, where)))
            return
        # Name admission comes BEFORE existence, and the order is the whole
        # guarantee. Checked after it, an undeclared tree that happens to be on
        # disk -- a leftover from a pin that was renamed, a hand-made directory,
        # a typo someone once staged -- resolves, passes, and the closed set
        # holds only for the names that are absent. That is a guarantee that
        # reads the same in both worlds and is true in one.
        parts = where.split("/")
        name = parts[1] if parts[0] == "staging" and len(parts) > 1 else None
        if name is not None and staged_names is not None and name not in staged_names:
            failures.append(
                "%s -> %s: staging/%s is not one of the trees stage-sources.sh "
                "writes (%s), so nothing pins what is there. It resolves today "
                "only because something left a directory behind."
                % (image_path, where, name, ", ".join(sorted(staged_names)) or "none")
            )
            return
        target = root / where
        if target.exists():
            checked.append((image_path, where))
            return
        if name is not None and not (root / "staging" / name).exists():
            skipped.append((image_path,
                            "%s into staging/%s, which is not staged here; "
                            "run ./stage-sources.sh to check it" % (kind, name)))
            return
        for sub in submodule_roots:
            if where.startswith(sub + "/") and not any((root / sub).iterdir()):
                failures.append(
                    "%s -> %s: submodule %s is not checked out, so this path "
                    "cannot be verified. Initialise it rather than passing "
                    "vacuously." % (image_path, where, sub)
                )
                return
        failures.append(
            "%s -> %s: named by %s, absent from the pinned tree"
            % (image_path, where, origin)
        )

    for keyword, body in instructions(text):
        if keyword != "RUN":
            continue
        for expanded in expand_loops(body):
            for image_path, kind in literals(expanded):
                audit(image_path, kind, dockerfile)
            for src in symlinked_plugins(expanded):
                what, where = resolve(src, sources, artifacts)
                if what != "repo":
                    continue
                init = root / where / "__init__.py"
                if init.exists():
                    checked.append((src + "/__init__.py", str(init.relative_to(root))))
                    continue
                failures.append(
                    "%s: linked into KiCad's plugin directory but holds no "
                    "__init__.py, so it is not a plugin package. KiCad scans that "
                    "directory for packages that register an ActionPlugin and "
                    "would find nothing, silently." % src
                )

    # The wrapper scripts under bin/ get the same treatment, and they matter
    # more: they are copied into /usr/local/bin and shipped, so a path that
    # went stale in one of them is broken for a user of the image rather than
    # for the build. Two of the five monorepo-move breakages were here.
    for script in sorted((root / "bin").glob("*")):
        if not script.is_file():
            continue
        for line in script.read_text(errors="replace").splitlines():
            # Only this quoted installed-layout spelling has a known image
            # default. Keep auditing its target; do not skip it as a variable.
            line = line.replace('"${ADK_TOOLS:-/opt/adk-tools}/',
                                '"/opt/adk-tools/')
            for tok in TOKEN.findall(line):
                idx = max(tok.find(r) for r in IMAGE_ROOTS if r in tok)
                audit(tok[idx:].rstrip("/"), "path", "bin/" + script.name)

    return failures, checked, skipped


def self_test():
    """Each failure mode above, reproduced from the shapes that actually broke."""
    cases = []

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "tools" / "plugin" / "plugins" / "chiplet_export").mkdir(parents=True)
        (root / "tools" / "plugin" / "plugins" / "chiplet_export" / "__init__.py").touch()
        (root / "tools" / "plugin" / "plugins" / "chiplet_export" / "tests").mkdir()
        (root / "examples").mkdir()
        (root / "examples" / "demo.txt").touch()
        (root / "ci").mkdir()
        (root / "ci" / "pin-set.json").write_text(json.dumps({"repos": {
            "interposer-pnr": {"stage_name": "interposer-pnr"},
            "adk": {"stage_name": None}}}))

        good = """\
FROM ubuntu:24.04 AS runtime
COPY tools/plugin /opt/adk-tools/plugin
COPY examples /opt/adk-tools/examples
COPY --from=builder /opt/adk-tools/studio /opt/adk-tools/studio
RUN mkdir -p /usr/share/kicad/scripting/plugins \\
    && ln -s /opt/adk-tools/plugin/plugins/chiplet_export \\
             /usr/share/kicad/scripting/plugins/chiplet_export
RUN for p in chiplet_export; do \\
        cd "/opt/adk-tools/plugin/plugins/$p" \\
        && /opt/adk-tools/venv/bin/python3 -m pytest tests -q; \\
    done
RUN cat /opt/adk-tools/examples/demo.txt /opt/adk-tools/studio/build/x
"""

        def run(dockerfile_text, name, want_fail, want_substring=None,
                want_checked=()):
            (root / "Dockerfile").write_text(dockerfile_text)
            try:
                failures, checked, _ = check(root)
            except Exception as exc:  # a crash fails this case, by name
                cases.append((name, False, ["check raised %r" % exc]))
                return
            ok = bool(failures) == want_fail
            if ok and want_substring:
                ok = any(want_substring in f for f in failures)
            if ok and not want_fail:
                ok = len(checked) >= 4  # it must actually have resolved things
            if ok:
                ok = all(target in checked for target in want_checked)
            cases.append((name, ok, failures))

        run(good, "a Dockerfile whose paths all resolve passes", False)

        # A COPY of several files lands each at dst/<name>; folded COPYs keep
        # every path they carry audited, and a directory among them is refused.
        (root / "ci" / "check_a.py").touch()
        folded = good + ("COPY ci/check_a.py ci/pin-set.json /opt/adk-tools/ci/\n"
                         "RUN python3 /opt/adk-tools/ci/check_a.py /opt/adk-tools/ci/pin-set.json\n")
        run(folded, "each file of a multi-source COPY resolves at dst/<name>",
            False, want_checked=(("/opt/adk-tools/ci/check_a.py", "ci/check_a.py"),
                                 ("/opt/adk-tools/ci/pin-set.json", "ci/pin-set.json")))
        run(folded.replace("RUN python3 /opt/adk-tools/ci/check_a.py",
                           "RUN python3 /opt/adk-tools/ci/check_b.py"),
            "a path a multi-source COPY does not carry fails",
            True, "matches no COPY")
        run(folded.replace("ci/check_a.py ci/pin-set.json /opt",
                           "ci/check_a.py examples /opt"),
            "a directory beside other sources in one COPY fails",
            True, "names the directory examples")
        (root / "ci" / "check_a.py").unlink()

        # Wrapper-only targets must be checked even when Dockerfile RUNs do
        # not name them. On failure, require the wrapper's own origin so an
        # unrelated Dockerfile failure cannot mask a lost wrapper scan.
        (root / "bin").mkdir()
        wrapper_targets = {
            "adk-drc": "adk/klayout/drc/run_drc.py",
            "chiplet-export":
                "chiplet_kicad_plugin/plugins/chiplet_export/chiplet_export_cli.py",
        }
        wrappers = good + "COPY wrapper-targets /opt/adk-tools\n"
        for name, target in wrapper_targets.items():
            path = root / "wrapper-targets" / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            interpreter = ('"${ADK_TOOLS:-/opt/adk-tools}/venv/bin/python3"'
                           if name == "adk-drc" else "/usr/bin/python3")
            (root / "bin" / name).write_text(
                '#!/bin/sh\nexec %s "${ADK_TOOLS:-/opt/adk-tools}/%s" "$@"\n'
                % (interpreter, target))
        run(wrappers, "relocatable wrapper targets are actually checked", False,
            want_checked=tuple(("/opt/adk-tools/" + target,
                                "wrapper-targets/" + target)
                               for target in wrapper_targets.values()))
        for name, target in wrapper_targets.items():
            path = root / "wrapper-targets" / target
            path.unlink()
            run(wrappers + "RUN cat /opt/adk-tools/" + target + "\n",
                "missing %s target fails with wrapper origin" % name,
                True, "named by bin/" + name)
            path.touch()
        for script in (root / "bin").iterdir():
            script.unlink()

        run(good.replace("ln -s /opt/adk-tools/plugin/plugins/chiplet_export",
                         "ln -s /opt/adk-tools/plugin/plugins"),
            "a symlink to a directory that is not a plugin package fails",
            True, "not a plugin package")

        run(good.replace("-m pytest tests", "-m pytest gone"),
            "a pytest target relative to a cd that does not exist fails",
            True, "gone")

        run(good.replace('cd "/opt/adk-tools/plugin/plugins/$p"',
                         'cd "/opt/adk-tools/plugin/moved/$p"'),
            "a stale cd target fails", True, "moved")

        # A RUN that cds into the image root and passes it bare, as the
        # tracked-tree tests' step does: the root names no path, and a pytest
        # target relative to it is still looked up.
        run(good + "RUN cd /opt/adk-tools \\\n"
                   "    && /opt/adk-tools/venv/bin/python3 -m pytest examples"
                   " --rootdir /opt/adk-tools -q\n",
            "a bare image root, as a cd target or an argument, is no path",
            False, want_checked=(("/opt/adk-tools/examples", "examples"),))

        run(good.replace("/opt/adk-tools/examples/demo.txt",
                         "/opt/adk-tools/examples/renamed.txt"),
            "a renamed file under an in-repo path fails", True, "renamed.txt")

        chmod = good + ("COPY --chmod=0444 ci/pin-set.json /opt/adk-tools/ci/pin-set.json\n"
                        "RUN cat /opt/adk-tools/ci/pin-set.json\n")
        run(chmod, "a COPY with a --chmod flag maps its source and destination", False,
            want_checked=(("/opt/adk-tools/ci/pin-set.json", "ci/pin-set.json"),))
        run(chmod.replace("COPY --chmod=0444 ci/pin-set.json", "COPY --chmod=0444 ci/pin-sets.json"),
            "a COPY with a --chmod flag and a renamed source fails", True, "pin-sets.json")

        run(good.replace("/opt/adk-tools/studio/build/x",
                         "/opt/adk-tools/unmapped/x"),
            "a path matching no COPY fails rather than being skipped",
            True, "matches no COPY")

        # staging/ is absent in a fresh clone, which is where CI runs. A
        # declared tree is skipped by name there; an undeclared one is a typo
        # and fails whether or not anything is staged.
        staged = good + ("COPY staging/interposer-pnr /opt/adk-tools/staging/interposer-pnr\n"
                         "RUN /opt/adk-tools/venv/bin/pip install -e "
                         "/opt/adk-tools/staging/interposer-pnr\n")
        run(staged, "a path into a declared but unstaged tree is skipped, not failed",
            False)

        run(staged.replace("staging/interposer-pnr", "staging/interposer-prn"),
            "a path into an undeclared staged tree fails even when nothing is staged",
            True, "is not one of the trees")

        # The same undeclared name, this time with a directory sitting there.
        # Existence must not buy admission, or the closed set is only closed
        # over the names that happen to be missing.
        (root / "staging" / "interposer-prn").mkdir(parents=True)
        run(staged.replace("staging/interposer-pnr", "staging/interposer-prn"),
            "an undeclared staged tree fails even when the directory exists",
            True, "is not one of the trees")
        (root / "staging" / "interposer-prn").rmdir()
        (root / "staging").rmdir()

        run(good.replace("COPY tools/plugin /opt/adk-tools/plugin\n", "")
                .replace("COPY examples /opt/adk-tools/examples\n", ""),
            "a Dockerfile with no source COPY fails instead of passing vacuously",
            True, "verified nothing")

        # An uninitialised submodule must be reported, not passed over.
        for child in (root / "tools" / "plugin").rglob("*"):
            if child.is_file():
                child.unlink()
        for child in sorted((root / "tools" / "plugin").rglob("*"), reverse=True):
            child.rmdir()
        run(good, "an uninitialised submodule fails rather than passing",
            True, "not checked out")

    ok = True
    for name, passed, got in cases:
        print("%-62s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            print("   checker returned: %r" % (got,))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    failures, checked, skipped = check(root)

    # Print what was not checked, always. A path check that quietly skips is
    # worse than none: it reads as coverage it does not have.
    for image_path, reason in sorted(set(skipped)):
        print("skip: %s (%s)" % (image_path, reason))
    if args.verbose:
        for image_path, repo_path in sorted(set(checked)):
            print("ok:   %s -> %s" % (image_path, repo_path))

    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("%d Dockerfile paths resolve in the pinned tree, %d not applicable"
          % (len(set(checked)), len(set(skipped))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
