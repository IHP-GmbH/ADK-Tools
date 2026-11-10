# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The shell of a Dockerfile's RUN, as this reader models it.

A RUN's shell is read into simple commands, each with the working directory it
runs in and its words with every variable the Dockerfile or the RUN itself
defines expanded; a word the reader cannot settle (a glob, a substitution, a
variable it does not know) keeps the part it can, so a caller can tell what the
word may name. What a caller cannot prove harmless from that is for it to refuse.

The reader models a closed part of the shell, the part the Dockerfile uses,
read as both dash and bash would run it. lex() says which words and operators
it reads: a RUN with any other is unread as a whole. A command is unread when
its verb is in REFUSED, it sets a variable for a builtin that may keep it
(KEEPS_ASSIGNS), it gives export an option, it runs a shell on a
string or with an option, it has a here-document, it may set a variable the
reader cannot name, or it is a set other than set -- or a set -e (u, x) that
is the first command of its shell. A variable the shell sets itself
(SHELL_SETS) is never settled. Commands are read by name: what a program does
beyond that is for a caller, and a program run under another's name is not
seen.

Shell.commands() reads a RUN; ci/dockerfile.py gives it the working directory
and the variables a step sees. ci/test_dockerfile.py is its test.
"""

import posixpath
import re
from dataclasses import dataclass, field, replace

from dockerfile import NAME, Unmodelled, resolve


@dataclass
class Word:
    """A word as the shell would see it, up to `at`: from there on it is unsettled."""
    text: str
    at: object = None  # None: settled
    start: int = 0
    end: int = 0
    splits: bool = False  # an unquoted expansion the reader cannot settle may split it

    @property
    def settled(self):
        return self.at is None

    @property
    def head(self):
        return self.text if self.settled else self.text[:self.at]

    def __str__(self):
        return self.text if self.settled else self.text[:self.at] + "..."


ANYTHING = Word("", 0)


@dataclass
class Command:
    """A simple command: the words from its verb on, the files it redirects
    output to, and the commands of a pipeline before it."""
    raw: str
    cwd: object  # str, or None when a cd the reader could not settle came first
    words: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    upstream: list = field(default_factory=list)
    error: str = None  # set when the reader could not read the command
    assigns: list = field(default_factory=list)  # (name, Word) set for the command alone
    environ: list = field(default_factory=list)  # (name, Word) of STEERS set, exported or not

    @property
    def said(self):
        return [w.text if w.settled else None for w in self.words]

    @property
    def name(self):
        """The verb's file name, None when the reader cannot settle it."""
        if self.error or not self.words or not self.words[0].settled:
            return None
        return posixpath.basename(self.words[0].text)


@dataclass(frozen=True)
class Options:
    """A command's options as this reader reads them."""
    letters: str = ""  # short options that take no value, alone or bundled (-rf)
    flags: frozenset = frozenset()  # other options that take no value, as whole words
    takes: frozenset = frozenset()  # options that take a value: the next word, or attached
    first: bool = False  # the options end at the first operand, as a wrapper's do
    any: bool = False  # an option not named here is read as one that takes no value, and a
    # word the reader cannot settle from its start as one too


def parse_options(words, spec):
    """(options, operands, rest) of words read as spec says: each option as
    (name, value Word or None), the words that are no option, and with
    spec.first the words from the first operand on. -- ends the options, and
    - is an operand unless spec names it. The options are None when a word
    is, or may be, an option spec does not read: one unknown, one the reader
    cannot settle from a -, or the words a split may make (ANYTHING). A word
    it cannot settle from its start is an operand, or with spec.any an
    option."""
    opts, operands, k = [], [], 0
    while k < len(words):
        w = words[k]
        s = w.text if w.settled else None
        if s == "--":
            k += 1
            break
        if (s == "-" and s not in spec.flags) or not (
                w.head.startswith("-") or w is ANYTHING or (spec.any and s is None and not w.head)):
            if spec.first:
                break
            operands.append(w)
            k += 1
            continue
        k += 1
        if s is None:
            if not spec.any:
                return None, operands, words[k - 1:]
            opts.append((None, None))
            continue
        name, value, long = s, None, s.startswith("--")
        if s in spec.takes:
            value, k = (words[k] if k < len(words) else ANYTHING), k + 1
        elif long and "=" in s and s.partition("=")[0] in spec.takes:
            name, _, rest = s.partition("=")
            value = Word(rest)
        elif not long and len(s) > 1 and s not in spec.flags:
            j = 1
            while j < len(s) and s[j] in spec.letters:
                j += 1
            opts += [("-" + c, None) for c in s[1:j]]
            if j == len(s):
                continue
            if "-" + s[j] in spec.takes:
                name = "-" + s[j]
                if j + 1 < len(s):
                    value = Word(s[j + 1:])
                else:
                    value, k = (words[k] if k < len(words) else ANYTHING), k + 1
            elif spec.any:
                name = s
            else:
                return None, operands, words[k - 1:]
        elif s not in spec.flags and not spec.any:
            return None, operands, words[k - 1:]
        opts.append((name, value))
    rest = list(words[k:])
    return (opts, operands, rest) if spec.first else (opts, operands + rest, [])


OPERATORS = ("&&", "||", ";;", ";", "&", "|", "(", ")", "\n")
REDIRECT = re.compile(r"\d*(?:>>|>\||>&|>|<<<|<<-?|<&|<>|<)")
WRITING = re.compile(r"\d*(?:>>|>\||>|<>)$")
KEYWORDS = {"!", "{", "}", "do", "done", "then", "else", "elif", "fi", "if", "while", "until"}
SHELLS = {"sh", "bash", "dash"}
# A word's text up to the blank or operator that ends it.
WORD_RUN = re.compile(r"[^\s;&|()<>]*")


def _wrapper(flags=(), takes=()):
    return Options(flags=frozenset(flags), takes=frozenset(takes), first=True)


# Commands that run the command after their own words, and the options of
# each this reader reads. Any other option, an abbreviated one too, is not read.
WRAPPERS = {
    "env": _wrapper({"-", "-i", "-0", "--ignore-environment", "--null"}, {"-u", "--unset"}),
    "nice": _wrapper(takes={"-n", "--adjustment"}),
    "nohup": _wrapper(),
    "exec": _wrapper({"-c", "-l"}, {"-a"}),
    "timeout": _wrapper({"--preserve-status", "--foreground", "-v", "--verbose"},
                        {"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": _wrapper(takes={"-i", "-o", "-e", "--input", "--output", "--error"}),
    "xargs": _wrapper({"-0", "-r", "-t", "-x", "--null", "--no-run-if-empty", "--verbose",
                       "--exit"},
                      {"-d", "-I", "-n", "-P", "-L", "-a", "-E", "-s", "--delimiter", "--replace",
                       "--max-args", "--max-procs", "--max-lines", "--arg-file", "--eof",
                       "--max-chars"}),
}
# command runs the command after its own words as the shell's own, and cd's
# options, as this reader reads them.
COMMAND = _wrapper({"-p", "-v", "-V"})
CD = Options("LPe@", first=True)
# Commands outside what this reader models, and why.
REFUSED = {
    **dict.fromkeys(("case", "esac", "select", "function", "coproc"),
                    "is a shell construct this reader does not read"),
    **dict.fromkeys(("su", "runuser", "sudo", "doas", "chroot", "flock", "setsid", "unshare",
                     "nsenter", "fakeroot", "watch", "parallel", "script", "ionice", "taskset",
                     "chrt", "busybox", "strace", "ltrace", "setpriv", "capsh", "systemd-run",
                     "fish", "zsh", "ksh", "mksh", "tcsh", "csh"),
                    "runs a command line this reader does not unwrap"),
    "eval": "evaluates a string", "alias": "defines an alias", "unalias": "removes an alias",
    "declare": "declares a variable", "typeset": "declares a variable",
    "let": "evaluates arithmetic", "[[": "is a test this reader does not read",
    "source": "sources a file", ".": "sources a file", "pushd": "changes directory",
    "popd": "changes directory", "trap": "sets a trap, which may run at any point",
    "hash": "changes what a name runs", "wait": "may set a variable (bash's wait -p)",
    "time": "is a keyword in bash and a program in dash",
    "builtin": "bash has and dash does not",
    "local": "dash refuses outside a function and bash ignores",
    "readonly": "makes a later assignment fail, and dash and bash differ on that",
    "mapfile": "may run a callback (-C)", "readarray": "may run a callback (-C)",
    "for": "is the for keyword only as written where a command starts",
}
# The builtins this reader follows, by the Shell method that does; a redirect of
# one still writes its target.
FOLLOWED = {"export": "_export", "set": "_set", "cd": "_cd"}
# Builtins that set the variables their words name.
SETS_NAMED = {"unset", "read", "getopts", "printf"}
# Builtins a value set for them alone may outlast: the special ones, and those
# that set variables or that this reader follows. A REFUSED one is refused anyway.
KEEPS_ASSIGNS = {":", "break", "continue", "exec", "exit", "return", "shift", "times",
                 "umask"} | SETS_NAMED | set(FOLLOWED)
# Variables the shell sets itself, or a builtin without naming them.
SHELL_SETS = {"_", "PWD", "OLDPWD", "OPTARG", "OPTIND", "REPLY", "MAPFILE", "RANDOM", "SRANDOM",
              "SECONDS", "LINENO", "BASHPID", "PPID", "PIPESTATUS", "BASH_REMATCH", "COPROC",
              "EPOCHSECONDS", "EPOCHREALTIME", "HISTCMD", "BASH_COMMAND", "FUNCNAME",
              "BASH_ARGV", "BASH_ARGC", "BASH_LINENO", "BASH_SOURCE", "DIRSTACK", "GROUPS"}
GLOB_OR_BLANK = re.compile(r"[\s*?\[]")
# Values in a command's environment that make it write elsewhere than its words
# say: a path (STEERS), or options or a configuration the program reads as its
# own (CONFIGURES).
CONFIGURES = re.compile(r"TAR_OPTIONS|UNZIP|UNZIPOPT|ZIPOPT|WGETRC|CURL_HOME")
STEERS = re.compile(r"GIT_\w+|DESTDIR|SIMPLE_BACKUP_SUFFIX|VERSION_CONTROL|"
                    + CONFIGURES.pattern)


def quoted(parts):
    """parts as they read in double quotes, or in an assignment's value: no
    part splits, and a glob or brace is its own character."""
    return [("q" + k, v) if k in ("var", "sub", "param") else (k, v)
            for k, v in parts if k not in ("glob", "brace")]


def _assignment(tok):
    """Whether a word token is NAME=VALUE or NAME+=VALUE."""
    return bool(tok[1]) and tok[1][0][0] == "lit" and re.match(NAME + r"\+?=", tok[1][0][1])


def _as_written(parts, raw):
    """parts, with the first marked 'qlit' unless raw (the word as written)
    begins with it verbatim, or with its NAME= or NAME+= for an assignment. A
    quote or an escape there makes the word neither keyword nor assignment,
    which the shell decides before it removes quotes."""
    if parts and parts[0][0] == "lit":
        m = re.match(NAME + r"\+?=", parts[0][1])
        if not raw.startswith(m.group(0) if m else parts[0][1]):
            parts[0] = ("qlit", parts[0][1])
    return parts


def _split_assignment(tok):
    """(name, whether it appends, value parts) of an assignment word:
    NAME+=VALUE's parts start with the old value; None when a tilde in the
    value expands, which the reader does not settle."""
    name, _, value = tok[1][0][1].partition("=")
    appends, name = name.endswith("+"), name.rstrip("+")
    if value.startswith("~") or ":~" in value:
        return name, appends, None
    parts = ([("lit", value)] if value else []) + list(tok[1][1:])
    return name, appends, [("qvar", name)] + parts if appends else parts


def _literal(tok):
    """A word token's text when it is one literal part, else None."""
    return tok[1][0][1] if len(tok[1]) == 1 and tok[1][0][0] == "lit" else None


def _vanishes(tok, w):
    """Whether a word is unquoted variables only, all empty: no word at all."""
    return bool(tok[1]) and w.settled and not w.text and all(k == "var" for k, _ in tok[1])


def _may_evaluate_subscript(args):
    """Whether a test with these words may evaluate an array subscript, which
    may run a command: bash's -v and -R take the word after them as a name,
    and a word the reader cannot settle may be either, or the name. A word
    that splits a value the reader holds is Shell.splits_known's; one that
    splits a value it does not is past the bound check_slice_tech_files'
    docstring states."""
    for w, name in zip(args, args[1:]):
        asks = w.text in ("-v", "-R") if w.settled else w.head in ("", "-", "-v", "-R")
        if asks and (not name.settled or "[" in name.text):
            return True
    return False


def _matching(text, i, opening, closing):
    """The index just past the bracket that closes the one before i."""
    depth, quote = 1, None
    while i < len(text) and depth:
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif text.startswith(opening, i):
            depth += 1
        elif text.startswith(closing, i):
            depth -= 1
            if not depth:
                return i + len(closing)
        i += 1
    raise Unmodelled("an unclosed %s" % opening)


def lex(text):
    """Operators, ('redir', op) pairs and words; a word is a list of parts:
    ('lit', s) (('qlit', s) when quoting makes it no keyword or assignment, see
    _as_written), ('var', name), ('sub', inner text), ('param', a positional or
    special parameter's name), ('glob', c) or ('brace', c) before the
    character itself, or ('tilde', '~'); in double quotes, where they do not
    split, ('qvar', ...), ('qsub', ...) and ('qparam', ...). Unmodelled for
    a form outside what this reader models (shell_reader's docstring)."""
    tokens, i, n = [], 0, len(text)
    parts, start = None, 0

    def lit(s):
        if parts and parts[-1][0] == "lit":
            parts[-1] = ("lit", parts[-1][1] + s)
        else:
            parts.append(("lit", s))

    def dollar(i):
        """The part a $ at i opens, and the index after it."""
        if text[i + 1:i + 2] in ("'", '"'):
            raise Unmodelled("%s...%s quoting" % (text[i:i + 2], text[i + 1]))
        if text.startswith(("$((", "$["), i):
            raise Unmodelled("arithmetic expansion")
        if text.startswith("$(", i):
            j = _matching(text, i + 2, "(", ")")
            return ("sub", text[i + 2:j - 1]), j
        if text.startswith("${", i):
            j = text.find("}", i)
            if j < 0:
                raise Unmodelled("an unclosed ${")
            inner = text[i + 2:j]
            if not re.fullmatch(r"\w+", inner):
                raise Unmodelled("the form ${%s}" % inner)
            return ("param" if inner.isdigit() else "var", inner), j + 1
        m = re.match(r"[A-Za-z_]\w*|[0-9@*#?$!-]", text[i + 1:])
        if not m:
            return ("lit", "$"), i + 1
        name = m.group(0)
        return ("var" if re.fullmatch(r"[A-Za-z_]\w*", name) else "param", name), \
            i + 1 + len(name)

    while i < n:
        c = text[i]
        if parts is None:
            if c in " \t":
                i += 1
                continue
            if c == "#":
                i = text.find("\n", i) if "\n" in text[i:] else n
                continue
            if text.startswith(("&>", "|&"), i):
                raise Unmodelled("%s, which dash reads as two operators" % text[i:i + 2])
            m = REDIRECT.match(text, i)
            if m:
                tokens.append(("redir", m.group(0)))
                i = m.end()
                continue
            op = next((o for o in OPERATORS if text.startswith(o, i)), None)
            if op == "(" and text.startswith("((", i):
                raise Unmodelled("an arithmetic command (( ))")
            if op:
                tokens.append(op)
                i += len(op)
                continue
            parts, start = [], i
            if c == "~":
                parts.append(("tilde", "~"))
                i += 1
                continue
        if c in " \t\n|&;()<>":
            raw = text[start:i]
            if c in "<>" and re.fullmatch(r"\{%s(?:\[.*\])?\}" % NAME, raw, re.S):
                raise Unmodelled("%s%s, which bash reads as a variable to set" % (raw, c))
            tokens.append(("word", _as_written(parts, raw), start, i))
            parts = None
            continue
        if c == "\\":
            lit(text[i + 1:i + 2])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise Unmodelled("an unclosed quote")
            lit(text[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '$`"\\\n':
                    lit(text[i + 1])
                    i += 2
                elif text[i] == "$":
                    part, i = dollar(i)
                    if part[0] == "lit":
                        lit(part[1])
                    else:
                        parts.append(("q" + part[0], part[1]))
                elif text[i] == "`":
                    j = text.find("`", i + 1)
                    if j < 0:
                        raise Unmodelled("an unclosed backtick")
                    parts.append(("qsub", text[i + 1:j]))
                    i = j + 1
                else:
                    lit(text[i])
                    i += 1
            if i >= n:
                raise Unmodelled("an unclosed quote")
            i += 1
        elif c == "$":
            part, i = dollar(i)
            parts.append(part) if part[0] != "lit" else lit(part[1])
        elif c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                raise Unmodelled("an unclosed backtick")
            parts.append(("sub", text[i + 1:j]))
            i = j + 1
        elif text.startswith("{}", i):
            lit("{}")  # no brace expansion
            i += 2
        elif c in "*?[{}":
            whole = WORD_RUN.match(text, start).group(0)
            if whole not in ("[", "[[", "{", "}"):
                parts.append(("brace" if c in "{}" else "glob", c))
            lit(c)
            i += 1
        else:
            lit(c)
            i += 1
    if parts is not None:
        tokens.append(("word", _as_written(parts, text[start:n]), start, n))
    return tokens


@dataclass
class ShellState:
    """What lasts from one command of a RUN to the next."""
    cwd: object  # str, or None after a cd the reader could not settle
    errexit: bool = False  # set -e is in force


@dataclass
class AndOr:
    """An and-or list being read, from the directory it started in."""
    cwd: object
    maybe: bool = False  # in a compound command, which may run it or not
    moved: bool = False  # a cd ran in it
    assigned: set = field(default_factory=set)  # variables set in it
    late: set = field(default_factory=set)  # variables set where a command may not run
    after: bool = False  # a command of it may not have run: after && or ||, or a !

    def fresh(self, cwd):
        return AndOr(cwd, maybe=self.maybe)


class Shell:
    """Reads one RUN's text into its simple commands, in the order they run.
    lookup(name) gives a variable the Dockerfile defines; files maps a file a
    command wrote (`... > F`) to what a later `$(cat F)` reads, if known."""

    def __init__(self, lookup, files=None, environ=()):
        self.lookup, self.files = lookup, files if files is not None else {}
        self.environ = set(environ)  # the names the Dockerfile puts in a RUN's environment

    def known(self, name, local):
        """Whether a variable is set, to anything, as far as the reader can tell."""
        return name in local or self.lookup(name) is not None

    def word(self, parts, local, start=0, end=0):
        """The word parts make. An unquoted variable whose value holds a blank
        or a glob, or any once IFS is set, splits or globs into words this
        reader does not follow, and a brace expansion makes words of its own:
        the word may then be anything. An unquoted variable, parameter or
        substitution the reader cannot settle may split, and "$@" is a word
        per value: the word keeps its start and says it splits. A glob
        keeps the start before it, and says so only when that is nothing or
        a -, which may make an option. A tilde is one word, $HOME. A .. after
        where the word is unsettled may climb out of anything: the word may
        then name any path. A variable of SHELL_SETS is never settled."""
        text, at, splits = "", None, False
        for kind, value in parts:
            if kind in ("lit", "qlit"):
                got = value
            elif kind in ("var", "qvar"):
                got = None if value in SHELL_SETS else \
                    local[value] if value in local else self.lookup(value)
                if got is not None and kind == "var" and (
                        GLOB_OR_BLANK.search(got) or self.known("IFS", local)):
                    return Word(text, 0, start, end, True)
                splits = splits or (got is None and kind == "var")
            elif kind == "brace":
                return Word(text, 0, start, end, True)
            else:
                got = None
                head = text if at is None else text[:at]
                splits = splits or kind in ("sub", "param") or (
                    kind == "qparam" and value == "@") or (
                    kind == "glob" and (not head or head.startswith("-")))
            if got is None:
                if at is None:
                    at = len(text)
            else:
                text += got
        if at is not None and ".." in text[at:]:
            at = 0
        return Word(text, at, start, end, splits)

    def commands(self, text, cwd, local=None):
        """Yields each Command; local holds the RUN's own variables.

        A command of a pipeline, or of a list run with &, runs in a subshell:
        its cd and assignments do not last. In an and-or list a cd may fail
        and a command after && or || may not run, so a working directory a cd
        changed and a variable set after && or || are unsettled once the list
        ends, unless set -e is in force and the list is that one command (set
        -e does not stop at a command before && or ||, after !, or in a
        condition). A compound command (for, while, until, if, a { } group) is
        one element of its list, and what it runs may run or not: a directory
        or a variable it changes is unsettled when it closes. A ( ) subshell is
        read the same way: set -e does not hold in one whose status is tested."""
        local = {} if local is None else local
        try:
            tokens = lex(text)
        except Unmodelled as exc:
            yield Command(text.strip(), cwd, error="cannot be read: %s" % exc)
            return
        stack, cmd, pipe, compound = [], [], [], []
        state, chain, ran = ShellState(cwd), AndOr(cwd), False

        def nest():
            nonlocal chain
            for tok in cmd:
                word = _literal(tok) if tok[0] == "word" else None
                if word in ("for", "while", "until", "if", "{"):
                    compound.append(chain)
                    chain = AndOr(state.cwd, maybe=True)
                elif word in ("done", "fi", "}"):
                    if compound:
                        end_list()
                        chain = compound.pop()
                        if state.cwd != chain.cwd:
                            state.cwd, chain.moved = None, True
                elif word == "!":
                    chain.after = True
                elif word not in KEYWORDS:
                    return

        def close(piped, term=None):
            nonlocal cmd, pipe, ran
            if cmd:
                nest()
                sub = piped or bool(pipe)
                # The first command of the shell, run in it before anything else.
                first = not (ran or sub or stack or compound or chain.after) \
                    and term in (None, ";", "\n", "&&")
                ran = True
                st, loc = (replace(state), dict(local)) if sub else (state, local)
                for got in self.simple(text, cmd, st, loc, list(pipe), chain, first):
                    yield got
                    if piped and not got.error:
                        pipe.append(got)
                if sub:
                    state.errexit = st.errexit and state.errexit
            if not piped:
                pipe = []
            cmd = []

        def end_list():
            nonlocal chain
            if not (state.errexit and not chain.after and not chain.maybe):
                if chain.moved and state.cwd != chain.cwd:
                    state.cwd = None
                for name in chain.late:
                    local[name] = None
            chain = chain.fresh(state.cwd)

        def background():
            nonlocal chain
            state.cwd = chain.cwd
            for name in chain.assigned:
                local[name] = None
            chain = chain.fresh(state.cwd)

        for tok in tokens:
            if tok == "|":
                yield from close(True)
            elif tok in ("&&", "||"):
                yield from close(False, tok)
                chain.after = True
                if tok == "||" and chain.moved:
                    state.cwd = None  # the right side runs when the left failed
            elif tok == "&":
                yield from close(False, tok)
                background()
            elif tok in (";", "\n", ";;"):
                yield from close(False, tok)
                end_list()
            elif tok == "(":
                if cmd:
                    yield Command(text.strip(), state.cwd,
                                  error="has a function or array definition")
                    return
                stack.append((replace(state), dict(local), chain))
                chain = AndOr(state.cwd, maybe=True)
            elif tok == ")":
                yield from close(False, tok)
                if not stack:
                    yield Command(text.strip(), state.cwd, error="has an unmatched )")
                    return
                saved_state, saved, chain = stack.pop()
                state.cwd, state.errexit = saved_state.cwd, saved_state.errexit
                local.clear()
                local.update(saved)
            else:
                cmd.append(tok)
        yield from close(False)
        end_list()

    def assign(self, tok, local, chain):
        """Set the variable an assignment word names (_split_assignment). dash
        has no +=, so NAME+=VALUE alone may leave NAME or append to it."""
        name, appends, parts = _split_assignment(tok)
        local[name] = None if appends or parts is None else self.assigned(parts, local)
        chain.assigned.add(name)
        if chain.after or chain.maybe:
            chain.late.add(name)

    def value(self, tok, local):
        """(name, Word) of an assignment word before a verb."""
        name, _, parts = _split_assignment(tok)
        return name, ANYTHING if parts is None else self.word(quoted(parts), local)

    def splits_known(self, tok, local):
        """Whether a word splits a value the reader holds, or the positional
        parameters, either of which may make words such as -v and a subscript."""
        def held(name):
            return name not in SHELL_SETS and (
                local[name] if name in local else self.lookup(name)) is not None

        return any(kind == "var" and held(value) or kind in ("param", "qparam")
                   and value in "@*" for kind, value in tok[1])

    def simple(self, text, tokens, state, local, upstream, chain, first):
        words, redirs = [], []
        it = iter(tokens)
        for tok in it:
            if tok[0] == "redir":
                target = next(it, None)
                if target is None or target[0] != "word":
                    yield Command(text.strip(), state.cwd, error="has a redirect with no target")
                    return
                redirs.append((tok[1], target))
            else:
                words.append(tok)
        spans = [t[2:] for t in words] + [t[1][2:] for t in redirs]
        raw = text[min(s for s, _ in spans):max(e for _, e in spans)] if spans else ""
        cwd = state.cwd
        # Substitutions run first, in a copy of the RUN's variables.
        for t in [w for w in words] + [r[1] for r in redirs]:
            for kind, value in t[1]:
                if kind in ("sub", "qsub"):
                    yield from self.commands(value, cwd, dict(local))
        resolved = [self.word(t[1], local, t[2], t[3]) for t in words]
        writes = []
        for op, target in redirs:
            if op.startswith("<<") and op != "<<<":
                yield Command(raw, cwd, error="has a here-document")
                return
            if WRITING.match(op) or (op.endswith(">&") and not re.fullmatch(
                    r"\d+|-", self.word(target[1], local).text)):
                writes.append(self.word(target[1], local, target[2], target[3]))

        # Keywords, then assignments: alone they set the RUN's variables; before
        # a verb, only its own. Both are read as written, before expansion.
        while words and _literal(words[0]) in KEYWORDS:
            resolved, words = resolved[1:], words[1:]
        # for NAME in WORDS, or for NAME; (for NAME in "$@"): sets NAME to words
        # it does not settle, and runs nothing; for NAME do ... would read its
        # body here, which this reader does not.
        if words and _literal(words[0]) == "for":
            if len(words) > 2 and _literal(words[2]) != "in":
                yield Command(raw, cwd, error="has a for without in, which this reader "
                                              "does not read")
                return
            if writes:
                yield Command(raw, cwd, writes=writes)
            if len(resolved) > 1:
                local[resolved[1].text] = None
            return
        lead = 0
        while lead < len(words) and _assignment(words[lead]):
            lead += 1
        settings, words, resolved = words[:lead], words[lead:], resolved[lead:]
        kept = [k for k, (t, w) in enumerate(zip(words, resolved)) if not _vanishes(t, w)]
        words, resolved = [words[k] for k in kept], [resolved[k] for k in kept]
        if not resolved:
            for t in settings:
                self.assign(t, local, chain)
                if redirs:  # dash skips the assignments when a redirect fails
                    name, _, _ = _split_assignment(t)
                    local[name] = None
            if writes:
                yield Command(raw, cwd, writes=writes)
            return
        assigns = [self.value(t, local) for t in settings]
        n = self.command_prefix(resolved)
        if n is None:
            yield Command(raw, cwd, error="gives command an option this reader does not read")
            return
        resolved, words = resolved[n:], words[n:]
        split_known = any(w.splits and self.splits_known(t, local)
                          for t, w in zip(words, resolved))
        if not resolved:
            if writes:
                yield Command(raw, cwd, writes=writes)
            return
        verb = resolved[0].text if resolved[0].settled else None
        if assigns and verb in KEEPS_ASSIGNS:
            yield Command(raw, cwd, error="sets %s for the builtin %s, which may keep it"
                                          % (assigns[0][0], verb))
            return
        if verb in FOLLOWED:
            if writes:
                yield Command(raw, cwd, writes=writes)
            why = getattr(self, FOLLOWED[verb])(words, resolved, state, local, chain, first)
            if why:
                yield Command(raw, cwd, error=why)
            return
        if verb in SETS_NAMED:
            names = self.named(verb, resolved)
            if names is None:
                yield Command(raw, cwd, error="may set a variable this reader cannot name")
                return
            for name in names:
                local[name] = None
        resolved, piped, more = self.unwrap(resolved)
        if resolved is None:
            yield Command(raw, cwd, error="wraps its command in a way this reader does not read")
            return
        assigns += more
        name = posixpath.basename(resolved[0].text) if resolved and resolved[0].settled else None
        if name in REFUSED:
            yield Command(raw, cwd, error="has %s, which %s" % (name, REFUSED[name]))
            return
        if name in ("[", "test") and (split_known or _may_evaluate_subscript(resolved[1:])):
            yield Command(raw, cwd, error="has a test that may evaluate a subscript (bash's "
                                          "-v or -R)")
            return
        if name in SHELLS and not (len(resolved) > 1 and resolved[1].settled
                                   and not resolved[1].text.startswith(("-", "+"))):
            yield Command(raw, cwd, error="runs a shell on a string or its input, or with "
                                          "an option, which this reader does not read")
            return
        # A word that may split may be followed by words of any kind.
        resolved = [x for w in resolved for x in ((w, ANYTHING) if w.splits else (w,))]
        if piped:
            resolved = resolved + [ANYTHING]
        environ = [(n, self.word([("qvar", n)], local))
                   for n in sorted(self.environ | set(local)) if STEERS.fullmatch(n)]
        yield Command(raw, cwd, resolved, writes, upstream, assigns=assigns, environ=environ)

    # The builtins of FOLLOWED: each reads its words into the shell's state and
    # says why it cannot, or None.

    def _export(self, words, resolved, state, local, chain, first):
        if not all(_assignment(t) or w.settled and re.fullmatch(NAME, w.text)
                   for t, w in zip(words[1:], resolved[1:])):
            return "gives %s an option or a word this reader does not read" % resolved[0].text
        for t in words[1:]:
            if _assignment(t):
                self.assign(t, local, chain)
        return None

    def _set(self, words, resolved, state, local, chain, first):
        said = [w.text if w.settled else None for w in resolved[1:]]
        if first and said and all(s and re.fullmatch(r"-[eux]+", s) for s in said):
            state.errexit = any("e" in s for s in said)
        elif said[:1] != ["--"]:
            return "sets shell options other than a set -e that comes first, which this " \
                   "reader does not read"
        return None

    def _cd(self, words, resolved, state, local, chain, first):
        state.cwd = self.cd(resolved[1:], state.cwd, local)
        chain.moved = True
        return None

    @staticmethod
    def named(verb, words):
        """The variables a builtin of SETS_NAMED may set with these words, None
        when a word the reader cannot settle may name one. printf sets only the
        ones its -v options name; the others any word that is a NAME, and any
        suffix of an option word that is one (read -aX)."""
        words = Shell.printf_targets(words) if verb == "printf" else words[1:]
        if words is None:
            return None
        names = []
        for w in words:
            if not w.settled or "[" in w.text:  # bash evaluates a subscript
                return None
            s = w.text.lstrip("-")
            names += [s[j:] for j in range(len(s) if w.text.startswith("-") else 1)
                      if re.fullmatch(NAME, s[j:])]
        return names

    @staticmethod
    def printf_targets(words):
        """The words printf's -v options name, None when a word the reader
        cannot settle may be one."""
        targets, k = [], 1
        while k < len(words) and (not words[k].head or words[k].head.startswith("-")):
            opt = words[k]
            if not opt.settled:
                return None
            if opt.text == "-v":
                if k + 1 == len(words):
                    return None
                targets.append(words[k + 1])
                k += 2
            elif opt.text.startswith("-v"):
                targets.append(Word(opt.text[2:]))
                k += 1
            else:
                break
        return targets

    @staticmethod
    def command_prefix(words):
        """How many words before the verb are command, which runs it as the
        shell's own, and its options; all of them for command -v or -V, which
        only says what it is. None for an option this reader does not read."""
        n = 0
        while n < len(words) and words[n].settled and words[n].text == "command":
            opts, _, rest = parse_options(words[n + 1:], COMMAND)
            if opts is None:
                return None
            if any(name in ("-v", "-V") for name, _ in opts):
                return len(words)
            n = len(words) - len(rest)
        return n

    def cd(self, args, cwd, local):
        """The directory a cd with these words goes to, None when unsettled. A
        cd that resolves links (-P) lands where the reader cannot tell, and so
        does one with an option it does not read, and cd -."""
        opts, _, names = parse_options(args, CD)
        if opts is None or any(n in ("-P", "-e", "-@") for n, _ in opts):
            return None
        if not names or not names[0].settled or names[0].text in ("-", ""):
            return None
        target = names[0].text
        if not target.startswith(("/", ".")) and self.known("CDPATH", local):
            return None
        return resolve(cwd, target)

    def assigned(self, parts, local):
        """A variable's value, None when unsettled; `$(cat F)` reads what F holds."""
        if len(parts) == 1 and parts[0][0] in ("sub", "qsub"):
            m = re.fullmatch(r"\s*cat\s+(\S+)\s*", parts[0][1])
            return self.files.get(m.group(1)) if m else None
        w = self.word(quoted(parts), local)
        return w.text if w.settled else None

    @staticmethod
    def unwrap(words):
        """The words from the wrapped verb on, whether xargs feeds it more, and
        the (name, Word) assignments env makes; None for the words when a
        wrapper's own words cannot be read."""
        piped, assigns = False, []
        while words and words[0].settled and posixpath.basename(words[0].text) in WRAPPERS:
            name, rest = posixpath.basename(words[0].text), words[1:]
            while True:
                opts, _, rest = parse_options(rest, WRAPPERS[name])
                if opts is None or any(v is not None and not v.settled for _, v in opts):
                    return None, piped, assigns
                if name != "env" or not rest or not rest[0].settled or "=" not in rest[0].text:
                    break
                key, _, value = rest[0].text.partition("=")
                assigns.append((key, Word(value)))
                rest = rest[1:]
            if name == "timeout":
                rest = rest[1:]
            piped = piped or name == "xargs"
            words = rest
        return words, piped, assigns
