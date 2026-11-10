#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The tools image's key: everything the part of the Dockerfile above the thin-layer line builds from.

The Dockerfile is in two parts. Above the line that starts with MARKER are the
tools: apt from a fixed snapshot, KiCad, the libraries, the PDK slices and
models, the Nix engines, the venv's third-party packages. Below it, our code,
FROM the tools image. build.sh builds the tools image only when no image
carries this key, and tags it adk-tools:tools-<first 12 hex>.

The key is the sha256 of a canonical JSON object holding:

  - the instructions above the line, as Docker reads them: comment lines and
    blank lines dropped, continuations joined, so a comment edit is not a new
    key and any other edit is;
  - .dockerignore, which shapes every COPY;
  - each file or directory an instruction above the line reads from the build
    context (COPY and ADD without --from, RUN --mount=type=bind without from=):
    a file by its sha256 and exec bit, a directory by every file in it, and a
    submodule by the commit its checkout is at, which has to be clean, since
    the build copies the working tree and not the commit;
  - the value of each build argument declared above the line: the one passed
    with --build-arg, else its default. TOOLS_IMAGE is left out: it names the
    image this key tags.

What is below the line does not enter the key. ci/check_thin_layer.py holds
what may stand there.

Usage:
    tools_key.py [--root DIR] [--build-arg NAME=VALUE]... [--json]
    tools_key.py --self-test
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKER = "#### thin layer:"
NOT_KEYED = {"TOOLS_IMAGE"}
CHUNK = 1 << 20


class ToolsKeyError(Exception):
    pass


def split(text):
    """(above, below) of the one thin-layer line."""
    lines = text.split("\n")
    at = [i for i, line in enumerate(lines) if line.startswith(MARKER)]
    if len(at) != 1:
        raise ToolsKeyError("the Dockerfile has %d lines starting %r; it needs exactly one"
                        % (len(at), MARKER))
    return "\n".join(lines[:at[0]]), "\n".join(lines[at[0] + 1:])


def instructions(text):
    """Each instruction as one line, as Docker reads it: comment and blank
    lines dropped, continuations joined."""
    out, parts = [], []
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line = line.rstrip()
        if line.endswith("\\"):
            parts.append(line[:-1].strip())
            continue
        parts.append(line.strip())
        out.append(" ".join(p for p in parts if p))
        parts = []
    if parts:
        out.append(" ".join(p for p in parts if p))
    return out


def flags(words):
    """(leading --flags, the rest) of an instruction's words."""
    n = 0
    while n < len(words) and words[n].startswith("--"):
        n += 1
    return words[:n], words[n:]


def context_sources(insts):
    """Build-context paths the instructions read."""
    sources = []
    for inst in insts:
        keyword, _, body = inst.partition(" ")
        keyword = keyword.upper()
        if keyword in ("COPY", "ADD"):
            if body.lstrip().startswith("["):
                raise ToolsKeyError("a JSON-form %s is not read here: %s" % (keyword, inst))
            fl, rest = flags(body.split())
            if any(f.startswith("--from=") for f in fl):
                continue
            if len(rest) < 2:
                raise ToolsKeyError("cannot read the sources of: %s" % inst)
            for src in rest[:-1]:
                if "://" in src:
                    raise ToolsKeyError("%s from a URL has no content to key: %s" % (keyword, inst))
                sources.append(src)
        elif keyword == "RUN":
            fl, _ = flags(body.split())
            for f in fl:
                if not f.startswith("--mount="):
                    continue
                opts = dict(o.partition("=")[::2] for o in f[len("--mount="):].split(","))
                if opts.get("type", "bind") != "bind" or "from" in opts:
                    continue
                if "source" not in opts and "src" not in opts:
                    raise ToolsKeyError("a bind mount with no source mounts the whole context: %s"
                                    % inst)
                sources.append(opts.get("source") or opts.get("src"))
    return sources


def declared_args(insts):
    """{name: default or None} of every ARG, the first default seen."""
    args = {}
    for inst in insts:
        keyword, _, body = inst.partition(" ")
        if keyword.upper() != "ARG":
            continue
        name, eq, value = body.strip().partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ToolsKeyError("cannot read: %s" % inst)
        if args.get(name) is None:
            args[name] = value if eq else None
    return args


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def is_gitlink(root, rel):
    r = git(root, "ls-files", "--stage", "--", rel)
    return r.returncode == 0 and r.stdout.startswith("160000 ")


def entry(root, rel):
    if os.path.isabs(rel) or ".." in pathlib.PurePosixPath(rel).parts:
        raise ToolsKeyError("%s is not a path inside the build context" % rel)
    path = root / rel
    if path.is_symlink():
        return {"symlink": os.readlink(path)}
    if path.is_file():
        return {"sha256": file_digest(path), "exec": bool(path.stat().st_mode & 0o111)}
    if not path.is_dir():
        raise ToolsKeyError("%s does not exist; the build would stop at it" % rel)
    if is_gitlink(root, rel):
        head = git(path, "rev-parse", "HEAD")
        if head.returncode != 0 or git(path, "rev-parse", "--show-toplevel").stdout.strip() \
                != str(path.resolve()):
            raise ToolsKeyError("%s is a submodule with no checkout" % rel)
        dirty = git(path, "status", "--porcelain")
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise ToolsKeyError("%s differs from its commit %s; the build copies the working "
                            "tree, so the commit would not name what the tools hold"
                            % (rel, head.stdout.strip()[:12]))
        return {"commit": head.stdout.strip()}
    files = {}
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            p = pathlib.Path(dirpath) / name
            files[str(p.relative_to(path))] = entry(root, str(p.relative_to(root)))
    return {"tree": files}


def key_object(root, text, build_args):
    above, _ = split(text)
    insts = instructions(above)
    ignore = root / ".dockerignore"
    args = {}
    for name, default in sorted(declared_args(insts).items()):
        if name in NOT_KEYED:
            continue
        args[name] = build_args.get(name, default)
    return {
        "schema": "adk-tools-tools-key/1",
        "instructions": insts,
        "dockerignore": file_digest(ignore) if ignore.is_file() else None,
        "sources": {rel: entry(root, rel) for rel in sorted(set(context_sources(insts)))},
        "args": args,
    }


def key(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def parse_build_args(pairs):
    out = {}
    for pair in pairs:
        name, eq, value = pair.partition("=")
        if not eq or not name:
            raise ToolsKeyError("--build-arg %r is not NAME=VALUE" % pair)
        out[name] = value
    return out


def self_test():
    failed, cases = [], 0

    def case(name, ok):
        nonlocal cases
        cases += 1
        if not ok:
            failed.append(name)
            print("FAIL: %s" % name, file=sys.stderr)

    def raises(fn):
        try:
            fn()
        except ToolsKeyError:
            return True
        return False

    base = (
        "# header\n"
        "ARG JOBS=8\n"
        "ARG TOOLS_IMAGE=tools\n"
        "FROM ubuntu AS deps\n"
        "ARG SNAP=1\n"
        "RUN --mount=type=bind,source=apt,target=/run/apt \\\n"
        "    # a comment inside\n"
        "    sh /run/apt/x.sh\n"
        "COPY --chmod=0444 a.txt /a.txt\n"
        "COPY --from=deps /x /y\n"
        "ADD closure.tar /\n"
        "COPY sub /sub\n"
        "#### thin layer: below\n"
        "FROM ${TOOLS_IMAGE} AS runtime\n"
        "ARG MANIFEST_B64=e30K\n"
        "COPY code /code\n")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tools-key-"))
    try:
        run = lambda *a, cwd=tmp: subprocess.run(  # noqa: E731
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
             "-c", "protocol.file.allow=always", *a], cwd=cwd, check=True, capture_output=True)
        run("init", "-q")
        (tmp / "apt").mkdir()
        (tmp / "apt" / "x.sh").write_text("echo\n")
        (tmp / "a.txt").write_text("a\n")
        (tmp / "closure.tar").write_bytes(b"tar")
        (tmp / "code").mkdir()
        (tmp / "code" / "c.py").write_text("x = 1\n")
        (tmp / ".dockerignore").write_text("**/.git\n")
        sub = tmp / "sub"
        sub.mkdir()
        run("init", "-q", cwd=sub)
        (sub / "s.c").write_text("int s;\n")
        run("add", "s.c", cwd=sub)
        run("commit", "-q", "-m", "s", cwd=sub)
        run("add", "sub", "a.txt")

        def k(text=base, args=None):
            return key(key_object(tmp, text, args or {}))

        k0 = k()
        case("the key is stable", k0 == k())
        case("a comment above the line is not a new key",
             k(base.replace("# header\n", "# header, edited\n# more\n\n")) == k0)
        case("a comment inside a RUN is not a new key",
             k(base.replace("# a comment inside", "# another")) == k0)
        case("an instruction above the line is a new key",
             k(base.replace("sh /run/apt/x.sh", "sh /run/apt/x.sh -v")) != k0)
        case("anything below the line is not",
             k(base.replace("COPY code /code", "COPY code /code2\nRUN true")) == k0)
        (tmp / "code" / "c.py").write_text("x = 2\n")
        case("a file below the line is not", k() == k0)
        (tmp / "a.txt").write_text("b\n")
        case("a COPY source above the line is", k() != k0)
        (tmp / "a.txt").write_text("a\n")
        (tmp / "apt" / "x.sh").chmod(0o755)
        case("a mounted file's exec bit is", k() != k0)
        (tmp / "apt" / "x.sh").chmod(0o644)
        (tmp / "apt" / "y.pem").write_text("pem\n")
        case("a file added to a mounted directory is", k() != k0)
        (tmp / "apt" / "y.pem").unlink()
        (tmp / "closure.tar").write_bytes(b"tar2")
        case("an ADD archive is", k() != k0)
        (tmp / "closure.tar").write_bytes(b"tar")
        case("back to the same bytes is the same key", k() == k0)
        (tmp / ".dockerignore").write_text("**/.git\nx\n")
        case(".dockerignore is", k() != k0)
        (tmp / ".dockerignore").write_text("**/.git\n")
        case("a build argument declared above the line is", k(args={"JOBS": "16"}) != k0)
        case("its default passed explicitly is not", k(args={"JOBS": "8"}) == k0)
        case("one declared only below the line is not", k(args={"MANIFEST_B64": "x"}) == k0)
        case("TOOLS_IMAGE is not", k(args={"TOOLS_IMAGE": "adk-tools:tools-x"}) == k0)
        case("an argument with no default takes the passed value",
             k(base.replace("ARG SNAP=1", "ARG SNAP"), {"SNAP": "1"})
             != k(base.replace("ARG SNAP=1", "ARG SNAP"), {"SNAP": "2"}))
        (sub / "s.c").write_text("int t;\n")
        case("a dirty submodule is refused", raises(k))
        run("commit", "-q", "-am", "t", cwd=sub)
        case("a submodule at another commit is a new key", k() != k0)
        (tmp / "a.txt").unlink()
        case("a missing source is refused", raises(k))
        (tmp / "a.txt").write_text("a\n")
        case("no line is refused", raises(lambda: k(base.replace("#### thin layer: below\n", ""))))
        case("two lines are refused",
             raises(lambda: k(base + "#### thin layer: again\n")))
        case("a source outside the context is refused",
             raises(lambda: k(base.replace("COPY --chmod=0444 a.txt", "COPY ../a.txt"))))
        case("a COPY from a URL is refused",
             raises(lambda: k(base.replace("ADD closure.tar /", "ADD https://x/y.tar /"))))
        case("a bind mount of the whole context is refused",
             raises(lambda: k(base.replace("source=apt,", ""))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d/%d self-test cases passed" % (cases - len(failed), cases))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--build-arg", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--json", action="store_true", help="print the keyed object, not the key")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    root = pathlib.Path(a.root).resolve()
    try:
        obj = key_object(root, (root / "Dockerfile").read_text(), parse_build_args(a.build_arg))
    except ToolsKeyError as exc:
        print("tools_key: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(obj, sort_keys=True, indent=2) if a.json else key(obj))
    return 0


if __name__ == "__main__":
    sys.exit(main())
