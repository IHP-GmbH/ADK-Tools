# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Whether a command a RUN runs leaves a file of the image as it was.

leaves() reads one Command of ci/shell_reader.py against one path. A command
leaves the path when it reads only (READS), makes directories or touches
(MAKES) where the file already is, runs a git verb that keeps the work tree
(GIT_KEEPS), or a config or a clone into a directory it names whose words, read
from where git works, name nothing at or above the file, in either case with no
option, config or environment that may point git elsewhere or run a command
(git_reader.git_unread), is a find whose actions write nothing or run only a
program that reads (FIND_READS), takes a mode that keeps every read and search
bit (chmod), copies with options it reads to a destination that is not the file
or above it (cp), is a script the caller names and the path is outside
SYSTEM_DIRS, or names no path at or above the file and, unless it writes only
what it names (OPERANDS_ONLY), does not run in a directory at or above it where
that matters: everywhere for a command that writes into its directory from what
it is given (WRITES_HERE: an archive, a remote, a build file), and within what
put the file there for any other. A redirect writes its target, and a value set
for the command alone, or one in its environment that steers where it writes
(shell_reader.STEERS), that names the file or above it may make it write there;
one that configures the program (shell_reader.CONFIGURES) is refused wherever
it points. A backup suffix may put a file next to each one written; sed's, with
a / in it, anywhere. An ln writes only its links; aliases() names what a link
to the file's tree, or to a target the reader cannot settle, makes a later
write through it reach. A verb is read by its name, wherever it is run from.

A word the reader cannot settle counts as naming every path that starts with
its settled part, and one unsettled from its start as naming any path; a word
NAME=VALUE names VALUE too.

ci/test_leave_policy.py is its test.
"""

import posixpath
import re

from dockerfile import norm, resolve, under
from git_reader import GIT_KEEPS, git_call, git_unread
from shell_reader import ANYTHING, CONFIGURES, SHELLS, STEERS, Options, Word, parse_options

# Commands that write nothing but through a redirect.
READS = {"[", "test", "cat", "cut", "grep", "head", "tail", "wc", "tr", "echo",
         "printf", "sha256sum", "cmp", "diff", "ls", "stat", "readlink", "true", "false",
         ":", "exit", "basename", "dirname", "id", "unset", "umask"}
# Those find may run: programs, not what only the shell has.
SHELL_ONLY = {":", "exit", "unset", "umask"}
FIND_READS = READS - SHELL_ONLY
FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
               "-fprintf", "-fls"}
FIND_RUNS = {"-exec", "-execdir", "-ok", "-okdir"}
# Commands that make a path or its times and never replace or remove one.
MAKES = {"mkdir", "touch"}
# Commands that write only the paths their words name, wherever they run.
OPERANDS_ONLY = {"rm", "rmdir", "cp", "mv", "chmod", "chown", "chgrp", "sed", "sort", "uniq"}
# Commands that write into the directory they run in, by what they are given.
WRITES_HERE = {"tar", "bsdtar", "unzip", "cpio", "7z", "7za", "ar", "unsquashfs", "wget",
               "curl", "patch", "make", "ninja", "cmake", "meson", "rsync"}
SYSTEM_DIRS = ("/usr", "/etc", "/var", "/lib", "/lib32", "/lib64", "/libx32", "/bin", "/sbin",
               "/boot", "/tmp", "/run")
# A chmod mode that only adds, or takes away no read or search bit, leaves a
# file's bytes and who can read it.
CHMOD_KEEPS = re.compile(r"(?:[ugoa]*(?:\+[rwxXst]*|-[wst]*))(?:,[ugoa]*(?:\+[rwxXst]*|-[wst]*))*")
TARGET = frozenset({"-t", "--target-directory"})
# cp's options that copy bytes and modes as they are, never as a link or a
# backup, and ln's.
CP = Options("aLHPRrfpdvnxTiu", frozenset({
    "--archive", "--dereference", "--recursive", "--force", "--preserve", "--no-preserve=all",
    "--verbose", "--no-clobber", "--update", "--no-target-directory", "--no-dereference",
    "--one-file-system"}), TARGET)
LN = Options("sfnrTvLP", frozenset({
    "--symbolic", "--force", "--no-dereference", "--relative", "--no-target-directory",
    "--verbose", "--logical", "--physical"}), TARGET)


def _long_option_prefixes(*names):
    """A pattern for these long options, or any prefix of one past its --: a
    superset of what getopt_long takes, which refuses an ambiguous one."""
    return "|".join("--(?:%s)" % "|".join(n[2:k] for k in range(len(n), 2, -1)) for n in names)


BACKUP = re.compile(r"-[a-zA-Z]*[bS].*|(?:%s)(?:=.*)?"
                    % _long_option_prefixes("--backup", "--suffix"))
# sed -i or --in-place with a suffix.
SED_BACKUP = re.compile(r"-[a-zA-Z]*i.+|(?:%s)=.+" % _long_option_prefixes("--in-place"))
# What a rule answers when the operand and directory rules decide.
GENERIC = object()


def reaches(word, cwd, path, below=False):
    """Whether a word, read from cwd, may name path or a directory above it
    (or, with below, a path under it)."""
    head = word.head
    if not word.settled and not head:
        return True
    if not head.startswith("/"):
        if cwd is None:
            return True
        head = posixpath.join(cwd, head)
    if word.settled:
        return under(path, head) or (below and under(head, path))
    d, partial = posixpath.split(head)
    if partial in (".", ".."):
        return True
    base = norm(d)
    if below and (under(base, path) or norm(path).startswith(norm(head))):
        return True
    if not partial:
        return under(path, base)
    return norm(path).startswith(base.rstrip("/") + "/" + partial)


def operands(words):
    """The words a command may take as paths: those that are no option, the
    value of a --name=value option, of a NAME=VALUE word and what follows a
    short option's letter (sort -oFILE); every word after --."""
    out, done = [], False
    for w in words:
        head = w.head
        if done or head == "-" or not head.startswith("-"):
            out += _values(w)
            continue
        if w.settled and w.text == "--":
            done = True
            continue
        if head.startswith("--"):
            if "=" in head:
                cut = head.index("=") + 1
            elif w.settled:
                continue
            else:
                cut = len(head)
        elif len(w.text) > 2 or not w.settled:
            cut = 2
        else:
            continue
        out += _values(Word(w.text[cut:], None if w.settled else max(w.at - cut, 0)))
    return out


def _values(w):
    """w and, for a word KEY=VALUE (a.b=VALUE too), VALUE."""
    m = re.match(r"[\w.-]+=", w.head)
    if not m:
        return [w]
    cut = m.end()
    return [w, Word(w.text[cut:], None if w.settled else w.at - cut)]


def _backup(verb, args, cwd, path):
    """Why a command's options may make it write a backup by a suffix next to
    path or in the directory above it, or None. sed -i takes the suffix
    attached, and one with a / in it may name any directory. An option word
    the reader cannot settle may be any of these."""
    pattern = SED_BACKUP if verb == "sed" else BACKUP
    words = [w for w in args if w.head.startswith("-") and w.head != "-"
             and (not w.settled or pattern.fullmatch(w.text))]
    if not words:
        return None
    if verb == "sed" and any(not w.settled or "/" in w.text for w in words):
        return "sed with an option this reader cannot settle, or an -i suffix with a /, " \
               "may write its backup anywhere"
    dirname = posixpath.dirname(path)
    if any(reaches(w, cwd, dirname, True) for w in operands(args)):
        return "%s %s may make backups by a suffix in %s or above it" % (verb, words[0], dirname)
    return None


def _find(cmd, args, said, path, scripts, below):
    if not FIND_WRITES.intersection(said):
        return None
    for k, s in enumerate(said):
        if s in FIND_RUNS:
            run = args[k + 1] if k + 1 < len(args) else None
            if run is None or not run.settled or posixpath.basename(run.text) not in FIND_READS:
                return "runs %s from find, which this reader does not read" % (run or "a command")
    return GENERIC


def _shell(cmd, args, said, path, scripts, below):
    if args and args[0].settled and args[0].text in scripts \
            and not any(under(path, d) for d in SYSTEM_DIRS):
        return None
    return GENERIC


def _chmod(cmd, args, said, path, scripts, below):
    modes = operands(args)
    if modes and modes[0].settled and CHMOD_KEEPS.fullmatch(modes[0].text):
        return None
    return GENERIC


def _git(cmd, args, said, path, scripts, below):
    """A verb that keeps the work tree leaves it; a config (the system, global
    or a repository's own) and a clone (into the directory it names) write
    what their words name, read from where git works (-C). Any other verb is
    refused: the repository it works in may be at any directory above where it
    runs, / too (one a COPY or the base image brings)."""
    g = git_call(cmd)
    why = git_unread(g)
    if why:
        return why
    if g.verb in GIT_KEEPS and not any(s and s.startswith("--output") for s in said):
        return None
    if g.verb == "clone" and len(g.words) < 2:
        return "runs git clone into a directory its remote names, below %s" \
            % (g.cwd or "where it runs")
    if g.verb in ("config", "clone"):
        for w in operands(cmd.words[g.k + 1:]):
            if reaches(w, g.cwd, path, below):
                return "names %s, which may be %s or above it, and git %s writes it" \
                    % (w, path, g.verb)
        return None
    return "runs git %s, which may write the work tree of a repository at any directory " \
           "above %s" % (g.verb or "with no verb", g.cwd or "where it runs")


def _target(opts):
    return next((v for name, v in opts if name in TARGET), None)


def _cp(cmd, args, said, path, scripts, below):
    """cp writes its destination only; it reads the rest."""
    opts, names, _ = parse_options(args, CP)
    if opts is None:
        return GENERIC
    target = _target(opts)
    dest = target if target is not None else names[-1] if names else None
    if dest is None or reaches(dest, cmd.cwd, path, below):
        return "copies onto %s, which may be %s or above it" % (dest, path)
    return None


# Commands that may copy a link they read as a link, besides cp with an
# option that keeps links (CP_KEEPS_LINKS, or a recursive one without -L or -H).
COPIES_LINKS = {"mv", "tar", "bsdtar", "cpio", "rsync", "7z", "7za", "unsquashfs", "ar", "unzip"}
CP_KEEPS_LINKS = {"-P", "-d", "--no-dereference", "-a", "--archive"}


def link_copies(cmd):
    """Where cmd may put a copy of a link it reads, None for a place the
    reader cannot settle; [] when it copies no link as one. cp and mv put it
    in their destination, any other copier where it names or runs."""
    if cmd.name in ("cp", "mv"):
        opts, names, _ = parse_options(cmd.words[1:], CP)
        if opts is None:
            return [None]
        given = {name for name, _ in opts}
        if cmd.name == "cp" and not (given & CP_KEEPS_LINKS or given & {
                "-R", "-r", "--recursive"} and not given & {"-L", "-H", "--dereference"}):
            return []
        dest = _target(opts) or (names[-1] if names else None)
        return [_place(dest, cmd.cwd) if dest is not None else None]
    if cmd.name in COPIES_LINKS:
        return [_place(w, cmd.cwd) for w in operands(cmd.words[1:])] + [cmd.cwd]
    return []


def _place(word, cwd):
    return resolve(cwd, word.text) if word.settled else None


def made_links(cmd):
    """(target, the paths the link may be at, or None when unsettled) for each
    link an ln makes; without -T a link named as an existing directory lands
    in it. An option ln does not read makes every link unsettled."""
    opts, names, _ = parse_options(cmd.words[1:], LN)
    if opts is None:
        return [(ANYTHING, None)]
    target_dir = _target(opts)
    exact = any(name in ("-T", "--no-target-directory") for name, _ in opts)

    def into(d, t):
        return None if d is None or not t.settled else [norm(posixpath.join(d, posixpath.basename(
            t.text.rstrip("/"))))]
    if target_dir is not None:
        return [(t, into(_place(target_dir, cmd.cwd), t)) for t in names]
    if len(names) == 1:
        return [(names[0], into(cmd.cwd, names[0]))]
    if len(names) == 2:
        at = _place(names[1], cmd.cwd)
        return [(names[0], None if at is None else [at] if exact
                 else [at] + (into(at, names[0]) or []))]
    return [(t, into(_place(names[-1], cmd.cwd), t)) for t in names[:-1]]


def _ln(cmd, args, said, path, scripts, below):
    for _, at in made_links(cmd):
        if at is None:
            return "makes a link where this reader cannot settle"
        if any(under(path, a) for a in at):
            return "makes a link that may be %s or above it" % path
    return None


def aliases(cmd, path):
    """(names path has through a link cmd makes, links to a target the reader
    cannot settle that may name it, where those links are): a write through
    any of them reaches path. A symbolic link's target is read from the
    link's directory, or with -r from where ln runs."""
    named, wild, at_all = [], [], []
    if cmd.name != "ln":
        return named, wild, at_all
    opts = [name for name, _ in parse_options(cmd.words[1:], LN)[0] or ()]
    symbolic = "-s" in opts or "--symbolic" in opts
    relative = "-r" in opts or "--relative" in opts
    for target, at in made_links(cmd):
        for a in at or ():
            base = posixpath.dirname(a) if symbolic and not relative else cmd.cwd
            if not reaches(target, base, path):
                continue
            at_all.append(a)
            t = _place(target, base)
            if t is None:
                wild.append(a)
            else:
                named.append(norm(a + path[len(t):]))
    return named, wild, at_all


RULES = {"find": _find, "chmod": _chmod, "git": _git, "ln": _ln, "cp": _cp,
         **{s: _shell for s in SHELLS}}


def leaves(cmd, path, top, scripts=(), below=False, held=True):
    """None when the command leaves path as it was, else why it may not; top
    is where what put the file there wrote, scripts the script paths run with
    sh that write only below SYSTEM_DIRS. With below, a write under path
    counts too (path is then a link that may name the file). held is False in
    a stage that does not hold the file, where making it puts it there."""
    if cmd.error:
        return cmd.error
    why = redirected(cmd, path, below)
    if why or not cmd.words:
        return why
    if not cmd.words[0].settled:
        return "runs a command this reader cannot name"
    verb, args = posixpath.basename(cmd.words[0].text), cmd.words[1:]
    for name, w in cmd.assigns + cmd.environ:
        # For any verb: a script it runs may run the program that reads it.
        if CONFIGURES.fullmatch(name):
            return "runs %s with %s in its environment, which may make a program write " \
                   "anywhere" % (verb, name)
        if (w.settled or STEERS.fullmatch(name)) and reaches(w, cmd.cwd, path, below):
            return "sets %s=%s for %s, which may be %s or above it" % (name, w, verb, path)
    if verb in READS:
        return None
    if verb in MAKES:
        if held or not any(reaches(w, cmd.cwd, path, True) and not (
                w.settled and resolve(cmd.cwd, w.text) not in (None, path)
                and under(path, resolve(cmd.cwd, w.text))) for w in operands(args)):
            return None
        return "%s may make %s in a stage that does not hold it" % (verb, path)
    why = _backup(verb, args, cmd.cwd, path)
    if why:
        return why
    rule = RULES.get(verb)
    why = GENERIC
    if rule is not None:
        why = rule(cmd, args, [w.text if w.settled else None for w in args], path, scripts, below)
        if why is not GENERIC:
            return why
    cwd = cmd.cwd
    for w in operands(args):
        if reaches(w, cwd, path, below):
            return ("names %s, which may be %s or above it, and %s is not read as leaving it"
                    % (w, path, verb))
    if verb in OPERANDS_ONLY:
        return None
    if cwd is None:
        return "runs %s where the working directory is not settled" % verb
    if (under(path, cwd) or (below and under(cwd, path))) and (
            verb in WRITES_HERE or under(cwd, top)):
        return "runs %s in %s, at or above %s" % (verb, cwd, path)
    return None


def redirected(cmd, path, below=False):
    """Why a redirect of cmd may write path, or None."""
    for w in cmd.writes:
        if reaches(w, cmd.cwd, path, below):
            return "redirects output to %s, which may be %s or above it" % (w, path)
    return None
