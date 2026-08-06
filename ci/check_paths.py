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
    "/opt/adk-tools/venv": "created by the `python3 -m venv` RUN",
    "/opt/adk-tools/manifest.json": "written by the MANIFEST_B64 RUN",
    "/opt/adk-tools/.demo-tracked-snapshot": "copied by verify step 4a",
    "/opt/adk-tools/examples/two_die_interposer/.seed-version":
        "content hash written by the seed-version RUN",
}
# Matched exactly rather than as a subtree. `/opt/adk-tools` as a prefix
# exemption would swallow every unmapped path under it and turn this checker
# into the catch-all the list above exists not to be.
BUILT_EXACT = {
    "/opt/adk-tools": "the image prefix itself (COPY LICENSE NOTICE.md dest)",
}

# `*` is inside the character class on purpose: a glob has to arrive here whole
# so it can be reported as unchecked. Stripped off silently it becomes a plain
# directory that resolves, and the run then claims coverage it does not have.
TOKEN = re.compile(r"[A-Za-z0-9_./\-\$\{\}\*]*(?:/opt/adk-tools|/usr/local/bin)[A-Za-z0-9_./\-\$\{\}\*]*")
FOR_LOOP = re.compile(r"\bfor\s+(\w+)\s+in\s+([^;\n]+?)\s*;\s*do\b")


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
    """(source COPYs, artifact prefixes) as {image prefix: repo path} and {prefix: stage}."""
    sources, artifacts = {}, {}
    for keyword, body in instructions(dockerfile_text):
        if keyword != "COPY":
            continue
        body = " ".join(body.split())
        from_stage = None
        m = re.match(r"--from=(\S+)\s+(.*)", body)
        if m:
            from_stage, body = m.group(1), m.group(2)
        args = shlex.split(body)
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
        # A multi-source COPY (LICENSE NOTICE.md -> dir) names a directory that
        # BUILT_AT_RUNTIME covers; mapping it as a prefix would be wrong.
    return sources, artifacts


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
        if cd and cd.group(1).startswith(IMAGE_ROOTS):
            cwd = cd.group(1).rstrip("/")
            found.append((cwd, "cd target"))
            continue
        for tok in TOKEN.findall(chunk):
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
    sources, artifacts = copy_map(text)
    failures, checked, skipped = [], [], []

    if not sources:
        failures.append(
            "%s: no COPY into the image prefix was parsed, so this check would "
            "pass having verified nothing." % dockerfile
        )
        return failures, checked, skipped

    submodule_roots = {v for v in sources.values() if v.startswith("tools/")}

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
        target = root / where
        if target.exists():
            checked.append((image_path, where))
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

        def run(dockerfile_text, name, want_fail, want_substring=None):
            (root / "Dockerfile").write_text(dockerfile_text)
            failures, checked, _ = check(root)
            ok = bool(failures) == want_fail
            if ok and want_substring:
                ok = any(want_substring in f for f in failures)
            if ok and not want_fail:
                ok = len(checked) >= 4  # it must actually have resolved things
            cases.append((name, ok, failures))

        run(good, "a Dockerfile whose paths all resolve passes", False)

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

        run(good.replace("/opt/adk-tools/examples/demo.txt",
                         "/opt/adk-tools/examples/renamed.txt"),
            "a renamed file under an in-repo path fails", True, "renamed.txt")

        run(good.replace("/opt/adk-tools/studio/build/x",
                         "/opt/adk-tools/unmapped/x"),
            "a path matching no COPY fails rather than being skipped",
            True, "matches no COPY")

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
