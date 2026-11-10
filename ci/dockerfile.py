# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""A Dockerfile as written: its stages and each step with what it sees.

The model reads only what the text states. Each step of a stage carries the
working directory, ENV and ARGs docker gives it: ENV and WORKDIR come down from
the stage's base, an ARG is in scope from its declaration to the end of its
stage (one before the first FROM only in FROM lines, or where a stage declares
it again), --build-arg overrides a declared default, and ENV wins over an ARG
of the same name. Instruction words are read as docker's lexer reads them:
quotes and escapes removed, $NAME and ${NAME} expanded, and any other ${...}
form, or a name the Dockerfile does not settle, left unsettled (docker would
read it as empty, or from a build argument or a base image).

ci/shell_reader.py reads the shell of its RUNs. Other checks in ci/ read the
Dockerfile with their own parsers; this is the one to fold them into.

ci/test_dockerfile.py is the test of both.
"""

import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field

KNOWN_WORDS = {"FROM", "ARG", "ENV", "COPY", "ADD", "RUN", "WORKDIR", "LABEL", "USER",
               "SHELL", "ENTRYPOINT", "CMD", "EXPOSE", "HEALTHCHECK", "STOPSIGNAL",
               "VOLUME", "ONBUILD", "MAINTAINER"}
NAME = r"[A-Za-z_]\w*"
REFERENCE = re.compile(r"\{(%s)\}|(%s)" % (NAME, NAME))


class Unmodelled(Exception):
    """The Dockerfile states something this model does not read."""


def norm(path):
    """A path with . and .. and repeated or trailing slashes taken out."""
    path = posixpath.normpath(path)
    return "/" + path.lstrip("/") if path.startswith("/") else path


def under(path, top):
    path, top = norm(path), norm(top)
    return path == top or path.startswith(top.rstrip("/") + "/")


def resolve(cwd, text):
    """text read from the directory cwd, absolute; None when it is relative to
    a directory the reader could not settle (cwd None)."""
    if text.startswith("/"):
        return norm(text)
    return None if cwd is None else norm(posixpath.join(cwd, text))


def instructions(text):
    """(keyword, rest) per instruction, the way docker splits them: continuations
    joined, and comments and blank lines skipped, inside a continuation too.
    Unmodelled for an escape parser directive, which changes what continues a
    line, and for a heredoc, whose lines are no instructions."""
    lines = text.lstrip("\ufeff").splitlines()
    for line in lines:
        m = re.match(r"\s*#\s*(\w+)\s*=", line)
        if not m:
            break
        if m.group(1).lower() == "escape":
            raise Unmodelled("the parser directive %s" % line.strip())
    out, cur = [], None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            cur = (cur or "") + stripped[:-1] + " "
            continue
        cur = (cur or "") + stripped
        if re.search(r"<<(?!<)-?\s*['\"]?\w", cur):
            raise Unmodelled("a heredoc in %s" % cur[:60])
        word, _, rest = cur.partition(" ")
        out.append((word.upper(), rest.strip()))
        cur = None
    if cur:
        word, _, rest = cur.partition(" ")
        out.append((word.upper(), rest.strip()))
    return out


def split_words(text):
    """text split at blanks outside quotes, each piece as written (quotes kept)."""
    out, cur, quote, i = [], None, None, 0
    while i < len(text):
        c = text[i]
        if quote is None and c in " \t":
            if cur is not None:
                out.append(cur)
            cur = None
            i += 1
            continue
        cur = (cur or "") + c
        if c == "\\" and quote != "'" and i + 1 < len(text):
            cur += text[i + 1]
            i += 1
        elif c in "'\"" and quote in (None, c):
            quote = None if quote else c
        i += 1
    if quote:
        raise Unmodelled("an unclosed quote in %s" % text)
    if cur is not None:
        out.append(cur)
    return out


def expand(text, lookup):
    """One word as docker's lexer reads it: quotes and escapes removed, $NAME and
    ${NAME} replaced by lookup(NAME). Unmodelled when lookup does not settle a
    name (None) and for any other ${...} form."""
    out, quote, i = [], None, 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < len(text) and (quote is None or text[i + 1] in '"$\\'):
            out.append(text[i + 1])
            i += 2
            continue
        if c in "'\"" and quote in (None, c):
            quote = None if quote else c
            i += 1
            continue
        if c == "$":
            if re.match(r"[0-9@*#?$!-]", text[i + 1:i + 2]):
                raise Unmodelled("$%s is a parameter docker reads as empty" % text[i + 1])
            m = REFERENCE.match(text, i + 1)
            if text.startswith("${", i) and not (m and m.group(1)):
                raise Unmodelled("%s is a ${...} form this reader does not read"
                                 % text[i:text.find("}", i) + 1 or None])
            if m:
                got = lookup(m.group(1) or m.group(2))
                if got is None:
                    raise Unmodelled("$%s is not settled by the Dockerfile" % m.group(0))
                out.append(got)
                i = m.end()
                continue
        out.append(c)
        i += 1
    if quote:
        raise Unmodelled("an unclosed quote in %s" % text)
    return "".join(out)


def settle(text, lookup):
    """expand(text), or None when the Dockerfile does not settle it."""
    try:
        return expand(text, lookup)
    except Unmodelled:
        return None


def split_flags(rest):
    """The leading --flags of an instruction and the text after them."""
    words = rest.split()
    flags = []
    for w in words:
        if not w.startswith("--"):
            break
        flags.append(w)
    return flags, rest.split(None, len(flags))[-1] if flags else rest.strip()


def env_pairs(rest):
    """The (name, value as written) pairs of an ENV, LABEL or ARG instruction."""
    pieces = split_words(rest)
    if pieces and "=" not in pieces[0]:
        return [(pieces[0], rest.strip()[len(pieces[0]):].strip())]
    return [tuple(p.partition("=")[::2]) for p in pieces]


def copy_args(rest):
    """(flags, sources, destination) of a COPY or ADD, shell or JSON form, as written."""
    if "<<" in rest:
        raise Unmodelled("a heredoc")
    flags, remainder = split_flags(rest)
    if remainder.startswith("["):
        try:
            args = json.loads(remainder)
        except ValueError as exc:
            raise Unmodelled("arguments this reader cannot read (%s)" % exc) from None
    else:
        args = split_words(remainder)
    if len(args) < 2:
        raise Unmodelled("fewer than a source and a destination")
    return flags, args[:-1], args[-1]


def run_text(rest):
    """The shell text a RUN runs; an exec form is quoted into one."""
    text = split_flags(rest)[1]
    if text.lstrip().startswith("["):
        try:
            return " ".join(shlex.quote(a) for a in json.loads(text))
        except (ValueError, TypeError):
            raise Unmodelled("an exec form this reader cannot read") from None
    return text


# The stages.

@dataclass
class Step:
    word: str  # the instruction, or UNKNOWN with the word as rest
    rest: str
    cwd: object  # str, or None after a WORKDIR the reader could not settle
    env: dict  # ENV in force: name -> value, None when unsettled
    args: dict  # ARGs in scope, the same way
    shell: object = None  # a SHELL instruction in force; None for /bin/sh -c

    def lookup(self, name):
        for table in (self.env, self.args):
            if name in table:
                return table[name]
        return None

    def expand(self, text):
        return expand(text, self.lookup)

    def path(self, text):
        """An instruction's path word, absolute; Unmodelled when unsettled."""
        got = resolve(self.cwd, self.expand(text))
        if got is None:
            raise Unmodelled("%s is relative to a WORKDIR this reader could not settle" % text)
        return got


@dataclass
class Copy:
    """A COPY or ADD with its words expanded."""
    word: str
    flags: list
    srcs: list
    dst: str  # absolute
    into: bool  # a file source lands under its own name in dst
    frm: object = None  # the --from stage, as expanded

    @property
    def wildcard(self):
        return any(c in src for src in self.srcs for c in "*?[")

    def named(self, src):
        return norm(posixpath.join(self.dst, posixpath.basename(src.rstrip("/"))))

    def placements(self):
        """(source, where a directory source's contents land, where a file
        source lands) per source, the later first: a later source wins."""
        for src in reversed(self.srcs):
            yield src, self.dst, self.named(src) if self.into else self.dst


def copy_step(step):
    """The Copy a COPY or ADD step states; Unmodelled when it cannot be read."""
    flags, srcs, dst = copy_args(step.rest)
    frm = next((step.expand(f[len("--from="):]) for f in flags if f.startswith("--from=")), None)
    return Copy(step.word, flags, [step.expand(src) for src in srcs], step.path(dst),
                len(srcs) > 1 or step.expand(dst).endswith("/"), frm)


UNSETTLED = object()  # a FROM whose base the Dockerfile does not settle


@dataclass
class Stage:
    name: str
    base: object  # the base as expanded, or UNSETTLED
    parent: object  # the Stage it is FROM, or None for an image
    steps: list = field(default_factory=list)
    cwd: object = "/"
    env: dict = field(default_factory=dict)
    args: dict = field(default_factory=dict)  # not inherited: an ARG ends with its stage
    shell: object = None


class Dockerfile:
    def __init__(self, text, build_args=()):
        given = dict(pair.partition("=")[::2] for pair in build_args)
        self.stages, globals_, stage = {}, {}, None
        for word, rest in instructions(text):
            if word == "FROM":
                stage = self.open_stage(rest, globals_)
                continue
            if stage is None:
                if word == "ARG":
                    self.declare(rest, globals_, globals_, given, globals_.get)
                continue
            step = Step(word, rest, stage.cwd, stage.env, stage.args, stage.shell)
            if word not in KNOWN_WORDS:
                step.word, step.rest = "UNKNOWN", word
            stage.steps.append(step)
            # A step sees the state before it; each change is a new table.
            if word == "ARG":
                stage.args = dict(stage.args)
                self.declare(rest, stage.args, globals_, given, step.lookup)
            elif word == "ENV":
                stage.env = dict(stage.env)
                for name, value in env_pairs(rest):
                    stage.env[name] = settle(value, step.lookup)
            elif word == "WORKDIR":
                value = settle(rest, step.lookup)
                stage.cwd = None if value is None else resolve(stage.cwd, value)
            elif word == "SHELL":
                stage.shell = rest

    @staticmethod
    def declare(rest, table, globals_, given, lookup):
        """An ARG instruction's names into table: a --build-arg, else the
        default, else the value a global ARG of the name has."""
        for piece in split_words(rest):
            name, eq, default = piece.partition("=")
            if name in given:
                table[name] = given[name]
            elif eq:
                table[name] = settle(default, lookup)
            else:
                table[name] = globals_.get(name)

    def open_stage(self, rest, globals_):
        parts = [p for p in rest.split() if not p.startswith("--")]
        if not parts:
            raise Unmodelled("a FROM with no base")
        name = (parts[2] if len(parts) >= 3 and parts[1].upper() == "AS"
                else "#%d" % len(self.stages))
        base = settle(parts[0], globals_.get)
        parent = self.stages.get(base) if base is not None else None
        stage = Stage(name, UNSETTLED if base is None else base, parent)
        if parent is not None:
            stage.cwd, stage.env, stage.shell = parent.cwd, dict(parent.env), parent.shell
        self.stages[name] = stage
        return stage
