# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""git as a RUN's shell states it, read from one Command of ci/shell_reader.py.

git_call() reads a git command's global options, verb, options and words, and
git_unread() says why one may work on a repository other than the one it
names, run a command it is given, or make a later git command do either.

ci/test_leave_policy.py is its test.
"""

from dataclasses import dataclass

from dockerfile import resolve
from shell_reader import Options, parse_options

# git's options before its verb, and those of a verb this reader names: the
# ones that take the next word as their value.
GIT_GLOBAL = Options(takes=frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                                      "--config-env", "--attr-source"}), first=True, any=True)
GIT_VERB = Options(takes=frozenset({"--depth", "--branch", "-b", "-o", "--origin", "-j", "--jobs",
                                    "--reference"}), any=True)
# git verbs that read a work tree and leave it, and its FETCH_HEAD; then those
# that leave the work tree, setting up a repository or fetching into it too.
GIT_READS = {"rev-parse", "ls-tree", "cat-file", "log", "show", "describe", "status", "ls-files"}
GIT_KEEPS = GIT_READS | {"init", "remote", "fetch"}
GIT_FETCHES = {"fetch", "clone", "pull", "ls-remote"}
# The options of the git verbs that reach a remote this reader reads; any
# other (one that runs a command git is given, --upload-pack, -u) is not.
GIT_REMOTE = {"-q", "--quiet", "--no-tags", "--single-branch", "--no-checkout", "-n",
              "--shallow-submodules", "--depth", "--branch", "-b"}
GIT_REMOTE_VALUES = ("--filter=",)
# The options of git init this reader reads: none takes a template or a git directory.
GIT_INIT = {"-q", "--quiet"}
# git config: the options that read, those that say which file, and those that
# write, allowed for safe.directory only, which says which repositories git trusts.
GIT_CONFIG_GETS = {"--get", "--get-all", "--get-regexp", "--list", "-l"}
GIT_CONFIG_SCOPE = {"--global", "--system", "--bool", "--int", "--path"}
GIT_CONFIG_SETS = {"--add", "--unset", "--unset-all", "--replace-all"}


@dataclass
class GitCall:
    """A git command as this reader reads it."""
    cwd: object  # where it works, -C applied; None when unsettled
    other: list  # its global options other than -C, and any word before the verb it cannot settle
    k: int  # the index of the verb among the command's words
    verb: object  # None when absent or unsettled
    opts: list  # the verb's options, None for a word that may be one; a separate value skipped
    words: list  # its other words, None for one the reader cannot settle
    env: list  # the GIT_ names set for it or in its environment


def git_call(cmd):
    """The GitCall of a command whose verb is git."""
    opts, _, rest = parse_options(cmd.words[1:], GIT_GLOBAL)
    d, other = cmd.cwd, []
    for name, value in opts:
        if name == "-C":
            d = resolve(d, value.text) if value.settled else None
        else:
            other.append(name or "a word this reader cannot settle")
    k = len(cmd.words) - len(rest)
    verb = rest[0].text if rest and rest[0].settled else None
    opts, operands, _ = parse_options(rest[1:], GIT_VERB)
    return GitCall(d, other, k, verb, [name for name, _ in opts],
                   [w.text if w.settled else None for w in operands],
                   [n for n, _ in cmd.assigns + cmd.environ if n.startswith("GIT_")])


def git_unread(g):
    """Why a git command may work on a repository other than the one it names,
    run a command it is given, or make a later git command do either (a config
    it sets, a template it makes a repository from); None for one this reader
    reads. It does not depend on a path."""
    if g.other:
        return "gives git %s, which may make it work on another repository or run a command" \
            % g.other[0]
    if g.env:
        return "runs git with %s in its environment, which may make it work on another " \
               "repository or run a command" % g.env[0]
    verb = g.verb or ""
    allowed, values = (GIT_INIT, ()) if verb == "init" else (GIT_REMOTE, GIT_REMOTE_VALUES)
    if verb in GIT_FETCHES | {"init"} and any(
            s is None or (s not in allowed and not s.startswith(values)) for s in g.opts):
        return "gives git %s an option this reader does not read" % verb
    if verb == "config":
        opts = set(g.opts)
        if None not in opts and opts & GIT_CONFIG_GETS \
                and opts <= GIT_CONFIG_GETS | GIT_CONFIG_SCOPE:
            return None
        key = g.words[0] if g.words else None
        if None not in opts and opts <= GIT_CONFIG_SETS | GIT_CONFIG_SCOPE \
                and key is not None and key.lower() == "safe.directory":
            return None
        return "sets git config %s, which may make git run a command or write elsewhere" \
            % (key or "this reader cannot settle")
    return None
