#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Every layer sheet, .lyt and stackup packaging-flow names is in the image the Dockerfile declares.

ci/check_tech_files.py holds a built image to packaging-flow's technology table.
This answers the same question with no build and no network: each path
check_tech_files reads (check_tech_files.reads) is resolved through the
Dockerfile as committed at --rev to the pinned source that would put it in the
image, read from that source on this host, and laid out in a scratch tree where
the image would hold it. Then check_tech_files.check runs on the scratch tree
with the image's environment and the path table of the Studio source the image
builds, so the verdict is that check's own.

What puts a file in a stage, read as written (ci/dockerfile.py says how a
stage, its ARGs, ENV and WORKDIR are read, ci/shell_reader.py a RUN's shell):

  - FROM an earlier stage; a base that is no stage holds nothing, one the
    Dockerfile does not settle is refused;
  - COPY from the build context or --from an earlier stage, shell or JSON form,
    with --chown, --link or a --chmod that keeps the file readable (and its
    directories searchable); a directory source's contents land in the
    destination, a file into it when it ends in / or there are several
    sources; a later COPY whose source lacks the file leaves the earlier one's,
    and one whose single file source would land on it inside an existing
    directory is refused; a context file is the one committed at --rev, one
    inside a submodule the one at its gitlink (from the submodule's checkout,
    or the modules gitdir in a worktree that has none), and .dockerignore at
    --rev applies;
  - in a RUN, a git checkout: `git init D`, `git -C D remote add origin URL`,
    `git -C D fetch ... origin REF` once, then `git -C D checkout -q FETCH_HEAD --`
    naming paths, or fed them by `cut -c67- F` or `grep -v '^#' F | awk
    '{print $2}'` over a file of the stage; REF a full commit sha, from an ARG
    or read by `v="$(cat F)"` from a file `git -C D rev-parse FETCH_HEAD > F`
    wrote; each URL is read from the local clone --clone names, never fetched.

Every step after the one that put the file in its stage, and every step of a
stage a COPY took from that holds no such file, must be shown to leave it
(ci/leave_policy.py). An ADD at or above the file, an instruction the reader
does not know, a command it cannot read, a RUN under a SHELL instruction, a
umask in the RUN that checks the file out, a symbolic link where the file is,
and a ref that is not a sha are refused, never passed. KNOWN waives one exact
command of one stage for a file outside what it says the command writes, with
the reason.

Not seen, by the bound of a closed reader: what a program does beyond the
words, options and environment the reader models; a program run under
another's name (a link or a PATH entry that shadows one); a program that
writes the file by its own logic, from a script or code it is given (a sed `w`
command, an awk redirect, `python3 -c`, a build), rather than through a word
it is given, a redirect or its working directory; a variable the reader cannot
settle whose value climbs out of the directory its word starts in (`/opt/x/$V`
with V=../pdk), or that is an option (`cp "$V" F` with V=-t/opt/pdk, or
`[ $V ]` with V='-v a[...]' from an ARG); a git hook or config a command other than git puts in
place (a file under .git/hooks, /etc/gitconfig, ~/.gitconfig, git's template
directory) or a .git it makes below a checkout's top; a link made before the
file is in place, one a COPY or the base image brings, one a program other
than ln makes (a clone, an archive it unpacks), or one a program other than
cp, mv or an archiver copies, that a later command writes through; a symbolic
link in a COPY source that names something else; context bytes that
.gitattributes would change on checkout (eol, filters, LFS); what the base
image holds and the working directory and environment it sets.

Usage:
    check_slice_tech_files.py --clone URL=DIR... [--packaging-flow TREE]
        [--root DIR] [--rev REV] [--target STAGE] [--build-arg NAME=VALUE]...

The packaging-flow tree is imported on this host, so its own imports
(chiplet-spec's description and reference packages) must be on PYTHONPATH.
ci/test_slice_tech_files.py is its test.
"""

import argparse
import os
import pathlib
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_tech_files  # noqa: E402
from dockerfile import (UNSETTLED, Dockerfile, Unmodelled as Refused,  # noqa: E402
                        copy_step, norm, resolve, run_text, under)
from shell_reader import ANYTHING, Command, Shell  # noqa: E402
from git_reader import GIT_READS, git_call, git_unread  # noqa: E402
from leave_policy import (aliases, leaves, link_copies, made_links,  # noqa: E402
                          operands, reaches, redirected)

# Where the image's Studio source is, by stage: the one the runtime's Studio
# is built from (verify step 9 copies the same file out of this stage).
STUDIO_SOURCE = ("studio-builder", "/opt/adk-tools/chiplet-studio/src/formats/ChipletFormat.cpp")

CLOSURE = ("the archive holds only /nix/store paths: build.sh packs it from a nix/*-closure.txt "
           "list, and read_list refuses any line that is not /nix/store/<hash>-<name>")
NIX = ("/nix/store",)
# One exact command (or ADD) of one stage that reaches a checked file: what it
# may write, and why. It is waived only for a file outside what it writes; a
# step that differs by a word is read again, and refused.
KNOWN = {
    ("tools", "ADD cosim-closure.tar /"): (NIX, CLOSURE),
    ("tools", "ADD mixsim-closure.tar /"): (NIX, CLOSURE),
    ("tools", "ADD viewer-closure.tar /"): (NIX, CLOSURE),
    ("tools", "ADD vams-closure.tar /"): (NIX, CLOSURE),
    ("runtime", "tar --extract --file /run/host-closure.tar --directory / "
                "--numeric-owner --same-owner --same-permissions"): (NIX, CLOSURE),
    ("kicad-builder", "DESTDIR=/install ninja -C /build/kicad -j\"${JOBS}\" install"):
        (("/build/kicad", "/install/usr"),
         "KiCad is configured with CMAKE_INSTALL_PREFIX=/usr, so ninja writes its build tree "
         "and installs below /install/usr"),
    ("studio-builder", "cmake .. -DKLAYOUT_BUILD_DIR=/opt/adk-tools/chiplet-studio/extern/"
                       "klayout/bin-release"):
        (("/opt/adk-tools/chiplet-studio/build",),
         "cmake runs in Studio's build/, writes its build tree there and reads the source above"),
    ("runtime", "/opt/adk-tools/venv/bin/python3 -B /run/check_interposer_label.py --tree "
                "/opt/adk-tools/OpenIntM4TM2 --pin \"${INTERPOSER_PIN}\""):
        (("/opt/adk-tools/OpenIntM4TM2/.shipped-ref",),
         "ci/check_interposer_label.py writes the reader's STAMP (.shipped-ref) at the tree's "
         "top and reads the rest"),
    ("runtime", "/opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_die_views.py "
                "--listing /opt/adk-tools/die-views.sha256 --root /opt/adk-tools/die-views "
                "--expect-sha256 \"$DIE_VIEWS_SHA256\" --manifest /opt/adk-tools/manifest.json"):
        ((), "ci/check_die_views.py reads its listing, tree and manifest and writes nothing "
             "outside its self-test"),
}
# Scripts a RUN mounts and runs with sh, by the path it runs them at, that write
# only below leave_policy.SYSTEM_DIRS, with the reason.
SCRIPTS = {
    "/run/apt-snapshot/apt-snapshot.sh":
        "docker/apt-snapshot/apt-snapshot.sh runs apt-get against a snapshot; it writes apt's "
        "cache, lists and a mktemp directory, and Debian packages install only into the FHS "
        "system directories (Debian Policy 9.1), never /opt or a directory of the build's own",
}

COPY_FLAGS = ("--from=", "--chmod=", "--chown=", "--link")
SHA = re.compile(r"[0-9a-f]{40}")
MADE = "a directory a later step writes into"


def git(repo, *args, text=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=text)


class Source:
    """An object in a git repository: (repo dir, commit, path inside it)."""

    def __init__(self, repo, commit, rel):
        self.repo, self.commit, self.rel = repo, commit, rel.strip("/")

    def kind(self):
        """'blob', 'tree' or None; refuses a symbolic link."""
        if not self.rel:
            return "tree"
        r = git(self.repo, "ls-tree", self.commit, "--", self.rel)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        mode, kind, _ = r.stdout.split("\t")[0].split()
        if mode == "120000":
            raise Refused("%s is a symbolic link in %s at %s; this reader does not follow links"
                          % (self.rel, self.repo, self.commit[:12]))
        return kind

    def below(self):
        """(path relative to this tree, 'blob' or 'tree') for each entry under
        it; a gitlink is a tree, its files unread."""
        r = git(self.repo, "ls-tree", "-r", self.commit, "--", self.rel + "/" if self.rel else ".")
        if r.returncode != 0:
            raise Refused("cannot list %s in %s at %s" % (self.rel, self.repo, self.commit[:12]))
        out = []
        for line in r.stdout.splitlines():
            meta, name = line.split("\t", 1)
            rel = name[len(self.rel):].lstrip("/") if self.rel else name
            out.append((rel, "tree" if meta.split()[0] == "160000" else "blob"))
        return out

    def blob(self):
        r = git(self.repo, "cat-file", "blob", "%s:%s" % (self.commit, self.rel), text=False)
        if r.returncode != 0:
            raise OSError("cannot read %s:%s in %s" % (self.commit, self.rel, self.repo))
        return r.stdout

    def __repr__(self):
        return "%s@%s:%s" % (self.repo, self.commit[:12], self.rel)


class BuildContext:
    """The build context as committed at a revision, .dockerignore applied."""

    def __init__(self, root, rev="HEAD"):
        self.root = pathlib.Path(root)
        self.head = git(self.root, "rev-parse", "--verify", "-q", rev + "^{commit}").stdout.strip()
        if not self.head:
            raise Refused("%s holds no commit %s" % (self.root, rev))
        self.ignore = self.dockerignore()

    def dockerignore(self):
        shown = git(self.root, "show", "%s:.dockerignore" % self.head)
        rules = []
        for line in shown.stdout.splitlines() if shown.returncode == 0 else ():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            pattern = norm(line[1:] if negate else line).strip("/")
            if any(c in pattern for c in "[]\\"):
                raise Refused(".dockerignore pattern %r is not one this reader can read" % line)
            rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(
                r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
            rules.append((re.compile(rx + r"\Z"), negate))
        return rules

    def ignored(self, rel):
        """Docker's reading: the last rule that matches the path or a directory
        above it decides."""
        parts, out = rel.split("/"), False
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        for rx, negate in self.ignore:
            if any(rx.match(prefix) for prefix in prefixes):
                out = not negate
        return out

    def source(self, rel):
        """The Source a context path is, or None when the context lacks it."""
        rel = norm(rel).strip("/")
        if rel.startswith(".."):
            raise Refused("%s is outside the build context" % rel)
        if self.ignored(rel):
            return None
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            r = git(self.root, "ls-tree", self.head, "--", prefix)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            mode, kind, obj = r.stdout.split("\t")[0].split()
            if mode == "120000" and i < len(parts):
                raise Refused("%s is a symbolic link in the build context; this reader does not "
                              "follow links" % prefix)
            if mode == "160000":
                return Source(self.submodule(prefix, obj), obj, "/".join(parts[i:]))
        return Source(self.root, self.head, rel)

    def submodule(self, path, commit):
        """The repository holding a gitlink's objects: its checkout when that is
        its own top, else the superproject's modules gitdir (a worktree without
        submodules checked out). Never a fetch."""
        checkout = self.root / path
        top = git(checkout, "rev-parse", "--show-toplevel")
        if top.returncode == 0 and (
                os.path.realpath(top.stdout.strip()) == os.path.realpath(checkout)):
            repo = checkout
        else:
            names = git(self.root, "config", "--blob", "%s:.gitmodules" % self.head,
                        "--get-regexp", r"^submodule\..*\.path$")
            name = next((line.split()[0][len("submodule."):-len(".path")]
                         for line in names.stdout.splitlines()
                         if line.split()[1:] == [path]), path)
            common = git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            repo = pathlib.Path(common.stdout.strip()) / "modules" / name
        if git(repo, "cat-file", "-e", "%s^{commit}" % commit).returncode != 0:
            raise Refused("gitlink %s at %s is not in %s; init the submodule here"
                          % (path, commit[:12], repo))
        return repo


# A RUN's stated checkouts.

@dataclass
class Manifest:
    """A checkout list read from a file of the stage, a path per line by pick."""
    path: str
    pick: object


@dataclass
class Checkout:
    """A stated git checkout into one directory, and the commands that fill it."""
    url: str = None
    ref: str = None
    fetched: bool = False
    paths: list = field(default_factory=list)  # str or Manifest
    commands: list = field(default_factory=list)
    conflict: str = None  # set when the checkout is stated in a way this reader does not model
    cloned: bool = False  # a git clone, whose files this reader does not read
    gone: bool = False  # its .git may be gone or replaced: later git commands are not its


@dataclass
class Read:
    """A step with, for a RUN, its commands and the checkouts they state."""
    step: object
    commands: list = field(default_factory=list)
    dirs: dict = field(default_factory=dict)  # checkout dir -> Checkout
    copy: object = None  # a COPY or ADD's Copy, or why it cannot be read

    def checkout_of(self, cmd):
        """(dir, Checkout) when cmd is a checkout command of a stated checkout."""
        return next(((d, c) for d, c in self.dirs.items()
                     if any(x is cmd for x in c.commands)), None)


@dataclass
class Model:
    reads: list
    shipped: dict  # file -> the ref `git rev-parse FETCH_HEAD > file` wrote


def manifest(upstream):
    """The checkout list a stated pipeline reads from a file."""
    said = [c.said for c in upstream]
    if len(said) == 1 and said[0][:2] == ["cut", "-c67-"] and len(said[0]) == 3 and said[0][2]:
        return Manifest(said[0][2], lambda line: line[66:])
    if (len(said) == 2 and said[0][:3] == ["grep", "-v", "^#"] and len(said[0]) == 4
            and said[0][3] and said[1] == ["awk", "{print $2}"]):
        return Manifest(said[0][3], lambda line: line.split()[1])
    return Manifest(None, " | ".join(c.raw for c in upstream))


def stated(cmd, dirs, shipped):
    """Follow one command of a stated checkout; the one place a checkout is
    recognised. Any other git command that may change a checkout's tree or
    FETCH_HEAD, in its directory (the nearest repository above where git runs)
    or where the reader cannot tell, makes the checkout a conflict. A command
    other than git that may write a checkout's .git before its checkout makes
    it a conflict, and after it, or in a clone, leaves later git commands
    there to the repository above. A clone's git commands are its own: they
    write in it."""
    if cmd.name is None:
        return
    if cmd.name != "git":
        for d, info in dirs.items():
            if not info.gone and leaves(cmd, d + "/.git", d, SCRIPTS, below=True):
                if info.cloned or info.commands:
                    info.gone = True
                else:
                    info.conflict = info.conflict or "has %r write its .git" % cmd.raw
        return
    g = git_call(cmd)
    d, k, verb, opts, words = g.cwd, g.k, g.verb, g.opts, g.words
    why = git_unread(g)
    if why or d is None:
        why = why or "has a git %s where this reader cannot tell which repository it runs " \
                     "in" % verb
        for info in dirs.values():
            info.conflict = info.conflict or why
        return
    if verb in ("init", "clone") and words and words[-1] and (verb == "init" or len(words) >= 2):
        target = resolve(d, words[-1])
        if target is not None:
            dirs.setdefault(target, Checkout(cloned=verb == "clone"))
        return
    if verb in GIT_READS and not (verb == "rev-parse" and words == ["FETCH_HEAD"]):
        return
    owner = max((x for x in dirs if under(d, x) and not dirs[x].gone), key=len, default=None)
    if owner is None:
        return
    info, sub = dirs[owner], cmd.said[k:]
    if info.cloned:
        info.commands.append(cmd)
    elif d != owner:
        info.conflict = "runs %r, which this reader does not model" % cmd.raw
    elif verb == "remote" and words[:2] == ["add", "origin"] and len(words) == 3 and not opts:
        if info.url is not None:
            info.conflict = "adds origin twice"
        info.url = words[2]
    elif verb == "fetch":
        if info.fetched:
            info.conflict = "fetches twice, and which FETCH_HEAD a checkout reads is not modelled"
        elif words[:1] != ["origin"] or len(words) != 2:
            info.conflict = "fetches as %r, which this reader does not read" % cmd.raw
        info.fetched, info.ref = True, words[-1] if words else None
    elif sub[:3] == ["checkout", "-q", "FETCH_HEAD"] and "--" in sub:
        info.commands.append(cmd)
        for w in cmd.words[k + sub.index("--") + 1:]:
            if w is ANYTHING and cmd.upstream:
                info.paths.append(manifest(cmd.upstream))
            elif not w.settled:
                info.conflict = "checks out %s, a path this reader cannot settle" % w
            elif w.text.startswith(":") or any(c in w.text for c in "*?[\\"):
                info.conflict = "checks out the pathspec %s, which this reader does not read" % w
            elif w.text:
                info.paths.append(w.text)
    elif verb == "rev-parse":
        if len(cmd.writes) == 1 and cmd.writes[0].settled and info.ref:
            shipped[cmd.writes[0].text] = info.ref
    else:
        info.conflict = "runs git %s in the checkout, which this reader does not model" % verb


def read_step(step, shipped):
    read = Read(step)
    if step.word in ("COPY", "ADD"):
        try:
            read.copy = copy_step(step)
        except Refused as exc:
            read.copy = str(exc)
    if step.word != "RUN":
        return read
    if step.shell is not None:
        read.commands.append(Command(step.rest, step.cwd, error="runs under SHELL %s, which "
                                     "this reader does not read" % step.shell))
        return read
    try:
        text = run_text(step.rest)
    except Refused as exc:
        read.commands.append(Command(step.rest, step.cwd, error="is %s" % exc))
        return read
    shell = Shell(step.lookup, shipped, set(step.env) | set(step.args))
    for cmd in shell.commands(text, step.cwd):
        read.commands.append(cmd)
        stated(cmd, read.dirs, shipped)
    return read


def chmod_keeps(flags, tree):
    """Whether a COPY's --chmod leaves its files readable and, for a tree, its
    directories searchable."""
    for f in flags:
        if f.startswith("--chmod="):
            mode = f[len("--chmod="):]
            if not re.fullmatch(r"0?[0-7]{3}", mode) or any(
                    d not in ("57" if tree else "4567") for d in mode[-3:]):
                return False
    return True


@dataclass
class Links:
    """The links a stage's commands made that a write may reach the file
    through: named, a name the file has through one; wild, a link to a target
    the reader cannot settle; at, where every link the stage's commands made
    since the file is, whatever its target (/ for one the reader cannot place)."""
    named: list = field(default_factory=list)
    wild: list = field(default_factory=list)
    at: list = field(default_factory=list)

    def __bool__(self):
        return bool(self.named or self.wild)

    def copy(self):
        return Links(list(self.named), list(self.wild), list(self.at))

    def each(self):
        """(name, True when a write under it counts) for every name."""
        return [(a, False) for a in self.named] + [(a, True) for a in self.wild]

    def follow(self, cmd, path, writes=None):
        """Take in the links cmd makes; writes, when KNOWN says, is all it
        may write."""
        for a in [path] + self.named:
            self.add(*aliases(cmd, a))
        for a in list(self.wild):
            self.add([], *aliases(cmd, a)[1:])
        if cmd.name == "ln":
            self.add([], [], [a for _, at in made_links(cmd) for a in (at or ["/"])])
        elif self.at and link_copies(cmd) and any(self.names(c) for c in [cmd] + cmd.upstream):
            # A command that reads a link may copy it as one (cp -a, mv, tar).
            places = writes if writes is not None else [p or "/" for p in link_copies(cmd)]
            self.add([], places, places)

    def names(self, cmd):
        """Whether a word of cmd may name where a link is, above or under it."""
        return any(reaches(w, cmd.cwd, a, below=True)
                   for w in operands(cmd.words[1:]) for a in self.at)

    def top(self, name):
        """What stands for the file's top through a link: the highest link
        location name is under."""
        return min((a for a in self.at if under(name, a)), key=len, default="/")

    def add(self, named, wild, at):
        for have, more in ((self.named, named), (self.wild, wild), (self.at, at)):
            have += [m for m in more if m not in have]

    def climbed(self, cmd):
        """Why a word of cmd may climb out of a link with .., or None: the
        reader resolves .. by name, and through a link it lands elsewhere. A
        word it cannot settle is read up to where it can."""
        if not self.at:
            return None
        for w in [w for w in cmd.words if w is not ANYTHING] + list(cmd.writes):
            m = re.search(r"(^|/)\.\.(/|$)", w.head)
            if m is None:
                continue
            p = resolve(cmd.cwd, w.head[:m.start()] or ".")
            if p is None or any(under(p, a) for a in self.at):
                return "climbs out of a link with %s" % w
        return None


class Reader:
    def __init__(self, df, context, clones, known=KNOWN):
        self.df, self.context, self.clones, self.known = df, context, clones, known
        self.models, self.lists, self.links, self.steered = {}, {}, {}, {}

    def waived(self, stage, raw, path):
        """Whether KNOWN waives this command of this stage for path."""
        entry = self.known.get((stage, " ".join(raw.split())))
        return entry is not None and not any(under(path, w) for w in entry[0])

    def model(self, name):
        """Each step of a stage read, and the files its checkouts' refs were written to."""
        if name not in self.models:
            stage = self.df.stages[name]
            if stage.base is UNSETTLED:
                raise Refused("stage %s is FROM a base the Dockerfile does not settle" % name)
            shipped = dict(self.model(stage.parent.name).shipped) if stage.parent else {}
            self.models[name] = Model([read_step(s, shipped) for s in stage.steps], shipped)
        return self.models[name]

    def git_steered(self, stage):
        """Why a git command of the stage, or of one it is FROM, may make
        another run a command or work elsewhere (git_reader.git_unread), or
        None. A config or template it leaves acts on every git command after
        it, wherever."""
        if stage not in self.steered:
            parent = self.df.stages[stage].parent
            self.steered[stage] = (
                (self.git_steered(parent.name) if parent else None) or self._git_unread_in(stage))
        return self.steered[stage]

    def _git_unread_in(self, stage):
        """Why a git command of the stage itself is one git_unread refuses, or None."""
        for c in (c for r in self.model(stage).reads for c in r.commands if c.name == "git"):
            why = git_unread(git_call(c))
            if why:
                return "stage %s: %r %s" % (stage, c.raw, why)
        return None

    def provide(self, stage, path, want):
        """(Source or MADE, top) for path in stage, or None; refuses a step it cannot read."""
        reads, steered = self.model(stage).reads, self.git_steered(stage)
        if steered:
            raise Refused(steered)
        for i in range(len(reads) - 1, -1, -1):
            step = reads[i].step
            if step.word == "UNKNOWN":
                raise Refused("stage %s has the instruction %s, which this reader does not know"
                              % (stage, step.rest))
            if step.word == "ONBUILD":
                raise Refused("stage %s has ONBUILD" % stage)
            if step.word == "VOLUME":
                if "$" in step.rest:
                    raise Refused("VOLUME %s in stage %s names a variable, which this reader "
                                  "does not expand" % (step.rest, stage))
                try:
                    volumes = shlex.split(step.rest.strip("[]").replace(",", " "))
                except ValueError:
                    raise Refused("VOLUME %s in stage %s, which this reader cannot split"
                                  % (step.rest, stage)) from None
                if any(under(path, v) for v in volumes):
                    raise Refused("VOLUME %s in stage %s covers %s" % (step.rest, stage, path))
            got, later = None, ()
            if step.word in ("COPY", "ADD"):
                got, later = self.copy(stage, reads[i], path, want), reads[i + 1:]
            elif step.word == "RUN":
                got, later = self.checkout(stage, reads[i], path, want), reads[i:]
            if got is not None:
                self.links[stage, path] = self.after(stage, later, path, got[1],
                                                     got[2] if len(got) > 2 else None)
                return got[:2]
        parent = self.df.stages[stage].parent
        got = self.provide(parent.name, path, want) if parent else None
        self.links[stage, path] = self.after(stage, reads, path, got[1] if got else path,
                                             links=self.links.get((parent.name, path))
                                             if parent else None, held=got is not None)
        return got

    @staticmethod
    def copy_of(stage, read):
        """A COPY or ADD's Copy; refuses one this reader cannot read."""
        if isinstance(read.copy, str):
            raise Refused("%s %s in stage %s: %s"
                          % (read.step.word, read.step.rest, stage, read.copy))
        return read.copy

    def copy(self, stage, read, path, want):
        c, raw = self.copy_of(stage, read), "%s %s" % (read.step.word, read.step.rest)
        if not (under(path, c.dst) or under(c.dst, path)):
            return None
        if c.word == "ADD":
            if self.waived(stage, raw, path):
                return None
            raise Refused("%s in stage %s reaches %s" % (raw, stage, path))
        if c.wildcard:
            raise Refused("%s in stage %s is a wildcard" % (raw, stage))
        bad = [f for f in c.flags if not f.startswith(COPY_FLAGS)]
        if bad:
            raise Refused("%s in stage %s has %s" % (raw, stage, " ".join(bad)))
        order = list(self.df.stages)
        if c.frm is not None and (c.frm not in self.df.stages
                                  or order.index(c.frm) >= order.index(stage)):
            raise Refused("COPY --from=%s in stage %s is an image, not an earlier stage"
                          % (c.frm, stage))

        def got_at(src, rel, kind):
            inner = posixpath.join(src, rel) if rel else src
            if c.frm is not None:
                return self.provide(c.frm, norm(posixpath.join("/", inner)), kind)
            source = self.context.source(inner)
            return (source, None) if source is not None and source.kind() == kind else None

        if not c.into and c.dst != path and c.named(c.srcs[0]) == path \
                and got_at(c.srcs[0], "", "blob") is not None:
            raise Refused("%s in stage %s puts its file at %s when %s is a directory then, "
                          "which this reader does not know" % (raw, stage, path, c.dst))
        # A directory source's contents land at its first place, a file source
        # at its second; a path under a place is in a directory source.
        for src, tree_at, file_at in c.placements():
            for target in dict.fromkeys((tree_at, file_at)):
                if target != path and under(target, path):
                    if want == "tree":
                        return MADE, target
                    continue
                if not under(path, target):
                    continue
                rel = path[len(target):].lstrip("/")
                if target != (tree_at if rel or want == "tree" else file_at):
                    continue
                got = got_at(src, rel, want)
                if got is not None:
                    if not chmod_keeps(c.flags, tree=bool(rel) or want == "tree"):
                        raise Refused("%s in stage %s sets a mode that may take away reading %s"
                                      % (raw, stage, path))
                    return got[0], target
        return None

    def checkout(self, stage, read, path, want):
        for d, info in read.dirs.items():
            if not under(path, d):
                continue
            if info.conflict:
                raise Refused("the checkout into %s in stage %s %s" % (d, stage, info.conflict))
            rel = path[len(d):].lstrip("/")
            paths = self.listed(stage, info)
            if not (any(under(rel, p) for p in paths)
                    or (want == "tree" and any(under(p, rel) for p in paths))):
                continue
            if info.url is None or info.ref is None:
                raise Refused("the checkout into %s in stage %s has no stated remote and ref"
                              % (d, stage))
            if not SHA.fullmatch(info.ref):
                raise Refused("the checkout into %s in stage %s is at %s, not a commit sha; "
                              "a branch or tag in a local clone need not be what a build fetches"
                              % (d, stage, info.ref))
            clone = self.clones.get(info.url)
            if clone is None:
                raise Refused("no --clone for %s, which stage %s checks out at %s"
                              % (info.url, stage, info.ref))
            if git(clone, "cat-file", "-e", info.ref + "^{commit}").returncode != 0:
                raise Refused("the clone %s does not hold %s; this check never fetches"
                              % (clone, info.ref))
            source = Source(clone, info.ref, rel)
            return (source, d, info) if source.kind() == want else None
        return None

    def listed(self, stage, info):
        """The paths a checkout names, its manifests read once."""
        key = (stage, id(info))
        if key not in self.lists:
            paths = []
            for p in info.paths:
                paths += self.manifest_paths(stage, p) if isinstance(p, Manifest) else [p]
            self.lists[key] = [norm(p).strip("/") for p in paths]
        return self.lists[key]

    def manifest_paths(self, stage, manifest):
        if manifest.path is None:
            raise Refused("stage %s feeds a checkout from %s, which this reader does not read"
                          % (stage, manifest.pick))
        got = self.provide(stage, norm(manifest.path), "blob")
        if got is None:
            raise Refused("the checkout list %s is not in stage %s" % (manifest.path, stage))
        out = []
        for line in got[0].blob().decode().splitlines():
            if line.strip() and not line.startswith("#"):
                try:
                    out.append(manifest.pick(line))
                except IndexError:
                    raise Refused("the checkout list %s has the line %r, which names no path"
                                  % (manifest.path, line)) from None
        return out

    def after(self, stage, reads, path, top, provider=None, links=None, held=True):
        """Refuse a step that may write path, itself or through a link; top is
        where what put it there wrote, provider the checkout that did, if one
        did, links the Links to it the stage starts with, held False in a stage
        that does not hold path. Returns the Links it ends with."""
        links = links.copy() if links else Links()
        for read in reads:
            step = read.step
            if step.word == "UNKNOWN":
                raise Refused("stage %s has the instruction %s, which this reader does not know"
                              % (stage, step.rest))
            if step.word == "ADD":
                dst = self.copy_of(stage, read).dst
                if under(path, dst) and not self.waived(stage, "ADD " + step.rest, path):
                    raise Refused("ADD %s in stage %s unpacks or writes at or above %s"
                                  % (step.rest, stage, path))
            if step.word in ("COPY", "ADD") and links:
                lands = self.lands(stage, read)
                raw = "%s %s" % (step.word, step.rest)
                for a, deep in links.each():
                    if any(under(a, d) or (deep or kind == "tree") and under(d, a)
                           for d, kind in lands) and not self.waived(stage, raw, a):
                        raise Refused("%s in stage %s writes through a link that may name %s"
                                      % (raw, stage, path))
            providing = provider is not None and any(i is provider for i in read.dirs.values())
            for cmd in read.commands:
                why = self.writes(stage, read, cmd, path, top, provider, providing, held) \
                    or links.climbed(cmd)
                if why and not self.waived(stage, cmd.raw, path):
                    raise Refused("stage %s: %r %s" % (stage, cmd.raw, why))
                # A command in a directory at or below a link runs within
                # what put the file there, read through it.
                for a, deep in links.each():
                    why = leaves(cmd, a, links.top(a), SCRIPTS, below=deep, held=held)
                    if why and not self.waived(stage, cmd.raw, a):
                        raise Refused("stage %s: %r %s, a link that may name %s"
                                      % (stage, cmd.raw, why, path))
                entry = self.known.get((stage, " ".join(cmd.raw.split())))
                links.follow(cmd, path, entry[0] if entry else None)
        return links

    def lands(self, stage, read):
        """(path, 'blob' or 'tree') for what a COPY or ADD may write; a tree is
        anything under it."""
        c = self.copy_of(stage, read)
        if c.word == "ADD" or c.frm is not None or c.wildcard:
            return [(c.dst, "tree")]
        out = []
        for src, tree_at, file_at in c.placements():
            got = self.context.source(src)
            kind = got.kind() if got is not None else None
            if kind == "tree":
                out += [(posixpath.join(tree_at, rel), k) for rel, k in got.below()]
            elif kind == "blob":
                out.append((file_at, "blob"))
        return out

    def writes(self, stage, read, cmd, path, top, provider, providing, held=True):
        """Why cmd may write path, or None."""
        hit = read.checkout_of(cmd)
        if hit is not None:
            d, info = hit
            redirect = redirected(cmd, path)
            if redirect or not under(path, d) or info is provider:
                return redirect
            if info.cloned:
                return "runs git in the clone %s, which may write %s" % (d, path)
            if info.conflict:
                return info.conflict
            rel = path[len(d):].lstrip("/")
            if rel and not any(under(rel, p) for p in self.listed(stage, info)):
                return None
            return "checks out over %s" % path
        if providing and cmd.said[:1] == ["umask"]:
            return "sets a umask in the RUN that checks %s out" % path
        return leaves(cmd, path, top, SCRIPTS, held=held)


def run(root, dockerfile_text, pf_tree, clones, target="runtime", build_args=(),
        studio=STUDIO_SOURCE, importer=check_tech_files.import_technologies, rev="HEAD",
        known=KNOWN):
    """(report lines, notes, failures)."""
    try:
        df = Dockerfile(dockerfile_text, build_args)
    except Refused as exc:
        return [], [], ["the Dockerfile: %s" % exc]
    if target not in df.stages:
        return [], [], ["no stage %s in the Dockerfile" % target]
    techs, failures = check_tech_files.declared(pf_tree, importer)
    for tech in techs:
        for name, _ in check_tech_files.FIELDS:
            value = getattr(tech, name)
            if value is not None and os.path.isabs(value):
                failures.append("%s %s %s is absolute; this check reads only paths under an "
                                "image root (${VAR})" % (tech.id, name, value))
    if failures:
        return [], [], failures
    try:
        reader = Reader(df, BuildContext(root, rev), clones, known)
    except Refused as exc:
        return [], [], [str(exc)]
    env = {k: v for k, v in df.stages[target].env.items() if v is not None}
    try:
        studio_src = reader.provide(studio[0], studio[1], "blob")
        if studio_src is None:
            return [], [], ["stage %s holds no %s" % studio]
        table = check_tech_files.studio_table(studio_src[0].blob().decode())
    except (Refused, ValueError) as exc:
        return [], [], ["Studio's path table: %s" % exc]
    wanted = {}
    for path, kind in check_tech_files.reads(techs, env, table).items():
        wanted[norm(path)] = kind
    lines = []
    with tempfile.TemporaryDirectory() as tmp:
        for path, kind in sorted(wanted.items()):
            try:
                got = reader.provide(target, path, "blob" if kind == "file" else "tree")
            except Refused as exc:
                failures.append("cannot read the image's %s from the Dockerfile: %s" % (path, exc))
                continue
            if got is None:
                continue
            out = pathlib.Path(tmp + path)
            if kind == "dir":
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(got[0].blob())
                lines.append("%s <- %r" % (path, got[0]))
        if failures:
            return lines, [], failures
        scratch_env = {k: tmp + v if v.startswith("/") else v for k, v in env.items()}
        more, notes, failed = check_tech_files.check(pf_tree, scratch_env, table, importer)
        failed = [f.replace(tmp, "<image>") for f in failed]
    return lines + more, notes, failed


def main():
    here = pathlib.Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(here), help="the adk-tools checkout (default: this one)")
    ap.add_argument("--rev", default="HEAD",
                    help="the commit whose Dockerfile and build context are read (default: HEAD)")
    ap.add_argument("--packaging-flow",
                    help="the packaging-flow tree (default: ROOT/staging/packaging-flow)")
    ap.add_argument("--clone", action="append", default=[], metavar="URL=DIR",
                    help="a local clone of a repository a stage fetches")
    ap.add_argument("--target", default="runtime", help="the stage whose files are checked")
    ap.add_argument("--build-arg", action="append", default=[], metavar="NAME=VALUE")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    root = pathlib.Path(args.root).resolve()
    pf = pathlib.Path(args.packaging_flow or root / "staging" / "packaging-flow").resolve()
    clones = {}
    for pair in args.clone:
        url, eq, path = pair.partition("=")
        if not eq:
            ap.error("--clone takes URL=DIR, got %s" % pair)
        clones[url] = os.path.abspath(path)
    shown = git(root, "show", "%s:Dockerfile" % args.rev)
    if shown.returncode != 0:
        ap.error("%s has no Dockerfile at %s: %s" % (root, args.rev, shown.stderr.strip()))
    print("Dockerfile at %s, packaging-flow at %s" % (
        git(root, "rev-parse", args.rev).stdout.strip(),
        git(pf, "describe", "--always", "--dirty", "--abbrev=40").stdout.strip() or "no git"))
    lines, notes, failures = run(root, shown.stdout, pf, clones, args.target, args.build_arg,
                                 rev=args.rev)
    for line in lines + notes:
        print(line)
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("every layer sheet, its .lyt and stackup %s names is in the %s stage as the Dockerfile "
          "declares it" % (check_tech_files.MODULE, args.target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
