#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""ci/check_slice_tech_files.py end to end: a Dockerfile, its build context and
the repositories it checks out, built in a scratch directory, and the verdict.

ci/test_dockerfile.py and ci/test_leave_policy.py test the reading under it.

Usage: python3 -B ci/test_slice_tech_files.py
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_tech_files  # noqa: E402
from check_slice_tech_files import KNOWN, git, run  # noqa: E402


cases = []


def case(name):
    def register(fn):
        cases.append((name, fn))
        return fn
    return register


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def repo(path, files, links=()):
    path.mkdir(parents=True)
    sh("git", "init", "-q", cwd=path)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    for rel, to in links:
        os.symlink(to, path / rel)
    sh("git", "add", "-A", cwd=path)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "t", cwd=path)
    return git(path, "rev-parse", "HEAD").stdout.strip()


SHEET = check_tech_files.SHEET
STACKUP = check_tech_files.STACKUP
STUDIO = ('constexpr PathVarMarker kPathVarMarkers[] = {\n'
          '    { "PDK_ROOT", { "pdk" }, "t/klayout" },\n'
          '    { "STUDIO_ROOT", { "studio" }, "stackups" },\n};\n')
URL = "https://example.invalid/pdk.git"


def dockerfile_text(pdk_paths, ref, extra_tools="", extra_pdk=""):
    return (
        "ARG UBUNTU=ubuntu:24.04\n"
        "FROM ${UBUNTU} AS pdk\n"
        "ARG PDK_REF=%s\n"
        "RUN apt-get install git \\\n"
        "    && git init -q /pdk \\\n"
        "    && git -C /pdk remote add origin %s \\\n"
        "    && git -C /pdk fetch -q --depth 1 origin \"${PDK_REF}\" \\\n"
        "    && git -C /pdk checkout -q FETCH_HEAD -- \\\n"
        "        %s \\\n"
        "    && git -C /pdk rev-parse FETCH_HEAD > /pdk/.shipped-ref \\\n"
        "    && rm -rf /pdk/.git\n%s"
        "FROM ${UBUNTU} AS studio\n"
        "COPY studio /opt/s\n"
        "FROM ${UBUNTU} AS runtime\n"
        "ENV PDK_ROOT=/opt/pdk \\\n    STUDIO_ROOT=/opt/s\n"
        "COPY --from=pdk /pdk /opt/pdk\n"
        "COPY --from=studio /opt/s/stackups /opt/s/stackups\n%s"
        % (ref, URL, " ".join(pdk_paths), extra_pdk, extra_tools))


MODULE = ("from types import SimpleNamespace as T\n"
          "TECHNOLOGIES = (\n"
          "    T(id='t', layer_properties='%s/t/klayout/t.lyp',\n"
          "      stackup='${STUDIO_ROOT}/stackups/t.yaml'),\n)\n")


def scratch(tmp, dockerignore="", worktree=None, lyt_link=False, sheet_root="${PDK_ROOT}",
            context=None, links=()):
    """The repositories a Dockerfile of dockerfile_text reads, built in tmp; a
    function that checks one such Dockerfile against them."""
    tmp = pathlib.Path(tmp)
    files = {"t/klayout/t.lyp": SHEET, "LICENSE": "x\n", "t/klayout/x": "y\n"}
    if not lyt_link:
        files["t/klayout/t.lyt"] = "<technology/>\n"
    pdk = tmp / "pdk-clone"
    sha = repo(pdk, files, [("t/klayout/t.lyt", "x")] if lyt_link else ())
    pf = tmp / "pf"
    repo(pf, {"src/packaging_flow/__init__.py": "",
              "src/packaging_flow/technologies.py": MODULE % sheet_root})
    studio = tmp / "studio-src"
    repo(studio, {"src/formats/ChipletFormat.cpp": STUDIO, "stackups/t.yaml": STACKUP})
    root = tmp / "adk-tools"
    files = {"list.sha256": "%s  t/klayout/t.lyp\n%s  t/klayout/t.lyt\n" % ("0" * 64, "0" * 64),
             "x.tar": "", "t.lyt": "<technology/>\n"}
    if dockerignore:
        files[".dockerignore"] = dockerignore
    files.update(context or {})
    repo(root, files, links)
    sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(studio),
       "studio", cwd=root)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "s", cwd=root)
    if worktree:
        # A worktree has no submodule checked out; its objects are in the
        # main checkout's modules gitdir, unless worktree() takes that away.
        sh("git", "worktree", "add", "-q", "--detach", str(tmp / "wt"), cwd=root)
        worktree(root)
        root = tmp / "wt"

    def check(pdk_paths, extra_tools="", extra_pdk="", ref=None, known=KNOWN):
        return run(root, dockerfile_text(pdk_paths, ref or sha, extra_tools, extra_pdk), pf,
                   {URL: str(pdk)}, studio=("studio", "/opt/s/src/formats/ChipletFormat.cpp"),
                   known=known)
    return check


def world(tmp, pdk_paths, extra_tools="", extra_pdk="", ref=None, known=KNOWN, **setup):
    return scratch(tmp, **setup)(pdk_paths, extra_tools, extra_pdk, ref, known)


BOTH = ["LICENSE", "t/klayout/t.lyp", "t/klayout/t.lyt"]
LYT = "/opt/pdk/t/klayout/t.lyt"


@case("the declared slice passes, every file read from its pinned source")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        lines, _, failures = world(tmp, BOTH)
    return not failures and any(
        line.startswith("t sheet ${PDK_ROOT}/t/klayout/t.lyp") for line in lines)


@case("a sheet the checkout list does not name fails")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, ["LICENSE", "t/klayout/t.lyt"])
    return (len(failures) == 1 and "is not a file here" in failures[0]
            and "t sheet ${PDK_ROOT}/t/klayout/t.lyp" in failures[0])


@case("a sheet without its .lyt in the list fails")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, ["LICENSE", "t/klayout/t.lyp"])
    return len(failures) == 1 and "has no technology file <image>%s" % LYT in failures[0]


@case("a worktree without its submodules checked out reads them from the modules gitdir")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, worktree=lambda root: None)
    return not failures


@case("a gitlink whose commit is nowhere here is refused, never fetched")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH,
                               worktree=lambda root: shutil.rmtree(root / ".git" / "modules"))
    return failures and all("gitlink studio at" in f and "init the submodule here" in f
                            for f in failures)


@case("a directory in the list carries the files below it")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, ["t/klayout"])
    return not failures


@case("a checkout fed by a manifest in the stage reads its paths")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        extra = ("FROM pdk AS listed\nCOPY list.sha256 /list.sha256\n"
                 "RUN ref=\"$(cat /pdk/.shipped-ref)\" && git init -q /m"
                 " && git -C /m remote add origin %s"
                 " && git -C /m fetch -q --filter=blob:none origin \"${ref}\""
                 " && cut -c67- /list.sha256"
                 " | xargs -r -d '\\n' git -C /m checkout -q FETCH_HEAD --"
                 " && cd /m && sha256sum -c /x || true\n" % URL)
        tools = "COPY --from=listed /m/t/klayout/t.lyt %s\n" % LYT
        _, _, failures = world(tmp, ["LICENSE", "t/klayout/t.lyp"], tools, extra)
    return not failures


@case("a later COPY whose source lacks the file leaves the earlier one's")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, "COPY --from=studio /opt/s /opt/pdk/t\n")
    return not failures


@case("an unknown instruction on the way is refused")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, "FROBNICATE /opt/pdk\n")
    return failures and all("FROBNICATE, which this reader does not know" in f for f in failures)


# Each of these, after the files are in, may change the .lyt, the sheet or
# the stackup: (lines at the end of the runtime stage, a stage before it).
REFUSED = [
    "ADD x.tar /opt/pdk/t",
    "ADD x.tar /opt/pdk/",
    'ADD ["x.tar", "/opt/pdk/t/klayout"]',
    "COPY studio/* /opt/pdk/t/klayout/",
    "RUN sed -i s/x/y/ %s" % LYT,
    "RUN sed -ri s/x/y/ %s" % LYT,
    "RUN sed --in-place s/x/y/ %s" % LYT,
    "RUN sort -o %s /x" % LYT,
    "RUN sort -o%s /x" % LYT,
    "RUN uniq /x %s" % LYT,
    "RUN cp /x %s" % LYT,
    "RUN cp /x /y /opt/pdk/t/klayout/ && cp -t /opt/pdk /z",
    "RUN cp -rT /x /opt/pdk/t",
    "RUN rm -rf /opt/pdk/t",
    "RUN rm -rf /opt",
    "RUN chmod a-r %s" % LYT,
    "RUN tar -xf /x.tar -C /opt/pdk",
    "RUN rm -f /opt/pdk/t/klayout/*.lyt",
    "RUN for f in t.lyt; do rm -f /opt/pdk/t/klayout/$f; done",
    "RUN sh -c 'rm -f %s'" % LYT,
    "RUN bash -euo pipefail -c \"rm -f %s\"" % LYT,
    "RUN find /opt/pdk -name '*.lyt' | xargs rm -f",
    "RUN find /opt/pdk -name '*.lyt' -delete",
    "RUN find /opt/pdk -execdir rm {} +",
    "RUN (cd /opt/pdk/t/klayout && rm t.lyt)",
    "RUN cd /opt/pdk/t && python3 rewrite.py klayout",
    "WORKDIR /opt/pdk/t/klayout\nRUN make",
    "RUN echo x >%s" % LYT,
    "RUN echo x 1> %s" % LYT,
    "RUN echo x &>%s" % LYT,
    "RUN echo x >> %s" % LYT,
    "RUN true & rm -rf /opt/pdk/t",
    "RUN export P=/opt/pdk/t && rm -rf $P",
    "RUN P=/opt/pdk && rm -rf \"$P/t\"",
    "RUN git -C /opt/pdk checkout -q HEAD -- t",
    "RUN $CMD /x",
    "RUN eval \"$X\"",
    "RUN bash -o pipefail -c \"$X\"",
    "RUN cd \"$X\" && make",
    "RUN case x in x) rm -rf /opt/pdk ;; esac",
    "RUN sh /run/other.sh /opt/pdk",
    "RUN sh -x /run/apt-snapshot/apt-snapshot.sh /opt/pdk",
    ("COPY --from=evil /e/ /opt/s/stackups/",
     "FROM ${UBUNTU} AS evil\nRUN mkdir /e && echo x > /e/t.yaml\n"),
    ("COPY --from=evil /e /opt/s/stackups",
     "FROM ${UBUNTU} AS evil\nCOPY studio/stackups /e\nRUN echo x > /e/t.yaml\n"),
    # A variable no ARG or ENV settles is empty to docker, never its own text.
    "ENV P=/opt/${NOPE}pdk\nRUN rm -rf $P",
    "WORKDIR /opt/${NOPE}pdk/t\nRUN rm -rf klayout",
    ("COPY --from=evil /e/ /opt/${NOPE}s/stackups/",
     "FROM ${UBUNTU} AS evil\nRUN mkdir /e && echo x > /e/t.yaml\n"),
    # Links, words and directories the shell does not read as written.
    "RUN ln -sT /opt/pdk/t /opt/other && rm -r /opt/other/*",
    "RUN ln -s %s /tmp/l && echo x > /tmp/l" % LYT,
    "RUN ln -s ../opt/pdk/t /opt/other\nRUN cp /dev/null /opt/other/klayout/t.lyt",
    "RUN ln -sT \"$X\" /opt/other && rm -r /opt/other/t",
    "RUN ln -sT /opt/pdk /opt/other\nCOPY t.lyt /opt/other/t/klayout/",
    "RUN ln -s /opt/pdk/t \"$X\"",
    "RUN P=\"/tmp /opt/pdk/t\" && rm -rf $P",
    "ENV CLEAN=\"/tmp/a /opt/pdk/t\"\nRUN rm -rf $CLEAN",
    "RUN cd /opt && tar -xf /x.tar",
    "RUN env -C /opt/pdk/t rm -rf klayout",
    "RUN env -S 'rm -rf /opt/pdk/t'",
    "RUN find /tmp -maxdepth 0 -exec sh -c 'rm -rf /opt/pdk/t' \\;",
    "RUN su -c 'rm -rf /opt/pdk/t' root",
    "RUN echo 'rm -rf /opt/pdk/t' | sh",
    "RUN bash -c \"rm -rf \\$'/opt/pdk/t'\"",
    "WORKDIR /opt/pdk/t/klayout\nRUN cd /nonexistent; rm -f t.lyt",
    "WORKDIR /opt/pdk/t\nRUN cd /tmp | true; rm -f klayout/t.lyt",
    "ENV P=/opt/pdk/t\nRUN true || P=/tmp; rm -rf \"$P\"",
    "ENV P=/x\nRUN unset P && rm -rf \"/opt/pdk/t$P\"",
    "RUN set -- /opt/pdk/t && rm -rf \"$@\"",
    "RUN . /etc/pdkenv && rm -rf /tmp/x",
    "RUN cd \"$(mktemp -d)\" && rm -rf klayout",
    "RUN cd /opt/pdk/t && \\\n\n    env rm -rf klayout",
    "WORKDIR \"/opt/pdk/t/klayout\"\nRUN rm -f t.lyt",
    "RUN git -C /tmp log --output=%s -1" % LYT,
    "RUN chmod -R go-x /opt/pdk",
    'SHELL ["/bin/bash", "-c"]\nRUN true',
    # ARG and ENV as docker scopes and expands them.
    "ARG TOP=/opt\nARG PDKD=${TOP}/pdk\nRUN rm -rf \"$PDKD/t\"",
    "ENV P=${UNSET:-/opt/pdk}\nRUN rm -rf \"$P/t\"",
    "ARG D='/opt/pdk/t'\nRUN rm -rf \"$D\"",
    "RUN rm -rf \"${UBUNTU}/opt/pdk/t\"",
    # A COPY that lands on the file, or takes reading it away.
    "COPY t.lyt /opt/pdk/t/klayout",
    "COPY --chmod=0600 --from=pdk /pdk /opt/pdk",
    # What a POSIX shell does not stop on, and names a write does not show.
    "WORKDIR /opt/pdk/t/klayout\nRUN set -e; if false; then cd /tmp; fi; rm -f t.lyt",
    "WORKDIR /opt/pdk/t/klayout\nRUN set -e; cd /nonexistent && true; rm -f t.lyt",
    "RUN ln -s /opt/pdk/t/klayout /tmp/k && rm -f /tmp/k/../klayout/t.lyt",
    "RUN GIT_WORK_TREE=/opt/pdk/t git --git-dir=/r/.git checkout -f HEAD -- klayout/t.lyt",
    "RUN git config --fil %s a.b c" % LYT,
    'VOLUME "/x',
    # A stage that does not hold the file makes it, itself or through a link.
    ("COPY --from=evil /e/ /opt/s/stackups/",
     "FROM ${UBUNTU} AS evil\nRUN mkdir /e && touch /e/t.yaml\n"),
    ("COPY --from=evil /e/t.yaml /opt/s/stackups/t.yaml",
     "FROM ${UBUNTU} AS evil\nRUN mkdir /e && ln -s /e /tmp/l && touch /tmp/l/t.yaml\n"),
    # A subshell whose status is tested, a cd the shell runs for its own.
    "WORKDIR /opt/pdk/t/klayout\nRUN set -e; (cd /nonexistent; rm -f t.lyt) || true",
    "RUN eval 'cd /opt/pdk/t' && rm -rf klayout",
    "RUN mkdir -p /opt/pdk/t/klayout/s && command cd /opt/pdk/t/klayout/s && rm -f ../t.lyt",
    # A word that splits, and .. through any link the stage made.
    "RUN for f in 'a /opt/pdk/t'; do rm -rf /tmp/$f; done",
    "RUN mkdir -p /opt/pdk/t/klayout/s && ln -s /opt/pdk/t/klayout/s /tmp/k && rm /tmp/k/../t.lyt",
    "RUN ln -s /opt/pdk/t /tmp/k && rm -rf /tmp/k/../t$(true)",
    "RUN ln -s /opt/pdk/t /tmp/k && cd /tmp/k && python3 x.py",
    # git told to work elsewhere, or set up to run a command.
    "ENV GIT_WORK_TREE=/opt/pdk/t\nRUN git -C /tmp/r checkout -f FETCH_HEAD -- klayout/t.lyt",
    "RUN git config --global core.hooksPath /tmp/h && git -C /tmp/r checkout -q x",
    "RUN git -C /opt/pdk/t/s checkout -q x",
    "ARG V=/x\nVOLUME $V",
    # A repository at /, and git's words read from where it works.
    "RUN git init -q /\nRUN git -C /tmp clean -fdxq ':/opt/pdk/t'",
    "WORKDIR /\nRUN git init -q && git clean -fdxq ':/opt/pdk/t'",
    "WORKDIR /w/x/y\nRUN git init -q /tmp/r"
    " && git -C /tmp/r archive -o ../../opt/pdk/t/klayout/t.lyt HEAD",
    # Values set for a string the shell runs, and links a copy keeps.
    "RUN P=/tmp/x; P=/opt/pdk/t sh -c 'rm -rf \"$P\"'",
    "RUN P=/tmp/x; P=/opt/pdk/t eval 'rm -rf \"$P\"'",
    "RUN ln -s /opt/pdk/t /tmp/k && cp -a /tmp/k /tmp/j && rm -f /tmp/j/klayout/t.lyt",
    "RUN ln -s /opt/pdk/t /tmp/k && tar -C /tmp -cf - k | tar -C /tmp/j -xf - && rm -rf /tmp/j/k",
    # Where the shell is after || exit, set --, a cd that resolves links.
    "RUN cd /tmp || exit 1 && cd /opt/pdk/t/klayout; rm -f t.lyt",
    "WORKDIR /opt/pdk/t/klayout\nRUN set -- -e; cd /nonexistent; rm -f t.lyt",
    "RUN mkdir -p /opt/pdk/t/klayout/s && ln -s /opt/pdk/t/klayout/s /tmp/k && cd -P /tmp/k"
    " && cd .. && rm -f t.lyt",
    "RUN bash -c 'pushd /opt/pdk/t/klayout && rm -f t.lyt'",
    "RUN bash -c 'P=/tmp/x; declare -n R=P; R=/opt/pdk/t; rm -rf \"$P\"'",
    "RUN bash -c 'P=/tmp/x1/opt/pdk/t; ((IFS=1)); rm -rf $P'",
    # Words a glob, a brace, $'...' or "$@" may make, and a .. after them.
    "RUN rm -rf /tmp/*/../../opt/pdk/t",
    "RUN cp {-t/opt/pdk/t/klayout,/tmp/a} /tmp/b",
    "RUN cp $'-t/opt/pdk/t/klayout' /tmp/b",
    "RUN set -- -t /opt/pdk/t/klayout; cp \"$@\" /tmp/t.lyt",
    # Outside the shell this reader models: ${...} forms, arithmetic, set
    # where it may not hold, an exit that may not end the shell, aliases.
    "RUN set -- -t /opt/pdk/t/klayout; cp \"${@}\" /tmp/t.lyt",
    "RUN : $((IFS<<=1)); P=/tmp/x0/opt/pdk/t; rm -rf $P",
    "RUN : ${X:-$((IFS=0))}; P=/tmp/x0/opt/pdk/t; rm -rf $P",
    "WORKDIR /opt/pdk/t/klayout\nRUN false && set -e; cd /tmp/nonexist; rm -f t.lyt",
    "WORKDIR /opt/pdk/t/klayout\nRUN ! cd /tmp/nonexist || exit; rm -f t.lyt",
    "RUN alias x='rm -rf /opt/pdk/t'; eval x",
    "RUN rm -rf /tmp/{.,}./opt/pdk/t",
    # A repository at / that a COPY brings, which git finds from anywhere.
    "COPY --from=studio /opt/s /.git\nRUN git -C /tmp reset -q --hard",
    "COPY --from=studio /opt/s /.git\nRUN find /tmp -maxdepth 0 -exec git reset -q --hard \\;",
    # A value a builtin keeps, dash's time, a trap, a redirect on a builtin,
    # an empty expansion that is no word.
    "RUN IFS=@ :; P=/tmp/@/opt/pdk/t; rm -rf $P",
    "RUN CDPATH=:/opt/pdk/t :; cd klayout && rm -f t.lyt",
    "WORKDIR /opt/pdk/t/klayout\nRUN time set -e; cd /tmp/nonexist; rm -f t.lyt",
    "WORKDIR /opt/pdk/t/klayout\nRUN trap 'cd /opt/pdk/t/klayout' CHLD; /bin/true; rm -f t.lyt",
    "RUN cd /tmp > /opt/pdk/t/klayout/t.lyt",
    "RUN set -e; HOME=/opt/pdk/t/klayout; V=; cd /tmp; cd $V; rm -f t.lyt",
    "RUN P=/opt/pdk/t/klayout; E=; $E P=/tmp/safe; rm -rf $P",
    "RUN E=; P=/opt/pdk/t/klayout $E; rm -rf $P",
    "WORKDIR /opt/pdk/t/klayout\nRUN cd /tmp &>/dev/null; rm -f t.lyt",
    "WORKDIR /opt/pdk/t/klayout\nRUN cd /tmp |& cat; rm -f t.lyt",
    "RUN fd=a/b; : {fd}>/dev/null; cd /opt/pdk/t/klayout/$fd/.. && rm -f t.lyt",
    "RUN OLDPWD=/tmp; cd /opt/pdk/t/klayout && cd /tmp && rm -f $OLDPWD/t.lyt",
    "RUN OPTARG=x; getopts a: o -a klayout; rm -rf /opt/pdk/t/$OPTARG",
    "RUN X=safe; printf -vX klayout; rm -rf /opt/pdk/t/$X",
    "RUN a=safe; read $Y < /tmp/f; rm -rf /opt/pdk/t/$a",
    "RUN a=safe; X=a; read $X < /tmp/f; rm -rf /opt/pdk/t/$a",
    "RUN hash -p /bin/rm ls; ls -rf /opt/pdk/t/klayout",
    "RUN X=safe; wait -p X; rm -rf /opt/pdk/t/$X",
    "RUN readonly X=/opt/pdk/t/klayout; X=/tmp/safe; rm -rf $X",
    "RUN TAR_OPTIONS=-C/opt/pdk/t tar -xf /tmp/a.tar",
    "RUN sed -i'/opt/pdk/t/klayout/*' -e p /tmp/t.lyt",
    "RUN sed -ilyt -e p /opt/pdk/t/klayout/t.",
    "RUN sed --in-place=lyt -e p /opt/pdk/t/klayout/t.",
    "RUN mapfile -C 'rm -rf /opt/pdk/t/klayout' -c 1 X < /tmp/f",
    "RUN X=/opt/pdk/t/klayout; \\X=/tmp/safe; rm -rf $X",
    "RUN X=/opt/pdk/t/klayout; \"X\"=/tmp/safe; rm -rf $X",
    "RUN X=/opt/pdk/t/klayout; X=/tmp/safe 2>/nonexist/f; rm -rf $X",
    "RUN cd /tmp && printf -v 'a[$(rm -rf /opt/pdk/t/klayout)]' x",
    "RUN cd /tmp && read 'a[$(rm -rf /opt/pdk/t/klayout)]' < /tmp/in",
    "RUN cd /tmp && [ -v 'a[$(rm -rf /opt/pdk/t/klayout)]' ]",
    "RUN sed '--i=/opt/pdk/t/klayout/*' -e p /tmp/t.lyt",
    "RUN sed --in=lyt -e p /opt/pdk/t/klayout/t.",
    "RUN mv --su=lyt /tmp/t.lyt /opt/pdk/t/klayout/t.",
    "RUN install --b --suf=lyt /tmp/t.lyt /opt/pdk/t/klayout/t.",
    "RUN X=safe; printf -v Y -v X klayout; rm -rf /opt/pdk/t/$X",
    "RUN cd /tmp && [ \"$O\" 'a[$(rm -rf /opt/pdk/t/klayout)]'\"$N\" ]",
    "RUN X=/opt/pdk/t/klayout; \"for\" X in /tmp/safe; rm -rf $X",
    "RUN fd=a/b; : {fd[0]}>/dev/null; cd /opt/pdk/t/klayout/$fd/.. && rm -f t.lyt",
    "RUN fd=a/b; : {fd[\" 0\"]}>/dev/null; cd /opt/pdk/t/klayout/$fd/.. && rm -f t.lyt",
    "RUN set -- x; for X do rm -f /opt/pdk/t/klayout/t.lyt; done",
    "RUN TAR_OPTIONS+=-C/opt/pdk/t tar -xf /tmp/a.tar",
    "RUN V='-v a[$(rm${IFS}-rf${IFS}/opt/pdk/t/klayout)]'; [ $V ] || true",
    "RUN set -- -v 'a[$(rm -rf /opt/pdk/t/klayout)]'; [ \"$@\" ] || true",
]
# What some of them are refused for, where any refusal would hide the one meant.
REASONS = {
    "RUN P=/opt/pdk/t/klayout; E=; $E P=/tmp/safe; rm -rf $P": "names /opt/pdk/t/klayout",
    "WORKDIR /opt/pdk/t/klayout\nRUN cd /tmp &>/dev/null; rm -f t.lyt": "&>",
    "WORKDIR /opt/pdk/t/klayout\nRUN cd /tmp |& cat; rm -f t.lyt": "|&",
    "RUN fd=a/b; : {fd}>/dev/null; cd /opt/pdk/t/klayout/$fd/.. && rm -f t.lyt": "{fd}>",
    "RUN hash -p /bin/rm ls; ls -rf /opt/pdk/t/klayout": "has hash",
    "RUN X=safe; wait -p X; rm -rf /opt/pdk/t/$X": "has wait",
    "RUN readonly X=/opt/pdk/t/klayout; X=/tmp/safe; rm -rf $X": "has readonly",
    "RUN TAR_OPTIONS=-C/opt/pdk/t tar -xf /tmp/a.tar": "TAR_OPTIONS",
    "RUN sed -i'/opt/pdk/t/klayout/*' -e p /tmp/t.lyt": "anywhere",
    "RUN sed -ilyt -e p /opt/pdk/t/klayout/t.": "backups",
    "RUN sed --in-place=lyt -e p /opt/pdk/t/klayout/t.": "backups",
    "RUN a=safe; read $Y < /tmp/f; rm -rf /opt/pdk/t/$a": "may set a variable",
    "RUN a=safe; X=a; read $X < /tmp/f; rm -rf /opt/pdk/t/$a": "names /opt/pdk/t",
    "RUN mapfile -C 'rm -rf /opt/pdk/t/klayout' -c 1 X < /tmp/f": "has mapfile",
    "RUN cd /tmp && printf -v 'a[$(rm -rf /opt/pdk/t/klayout)]' x": "may set a variable",
    "RUN cd /tmp && read 'a[$(rm -rf /opt/pdk/t/klayout)]' < /tmp/in": "may set a variable",
    "RUN cd /tmp && [ -v 'a[$(rm -rf /opt/pdk/t/klayout)]' ]": "subscript",
    "RUN sed '--i=/opt/pdk/t/klayout/*' -e p /tmp/t.lyt": "anywhere",
    "RUN sed --in=lyt -e p /opt/pdk/t/klayout/t.": "backups",
    "RUN mv --su=lyt /tmp/t.lyt /opt/pdk/t/klayout/t.": "backups",
    "RUN install --b --suf=lyt /tmp/t.lyt /opt/pdk/t/klayout/t.": "backups",
    "RUN X=safe; printf -v Y -v X klayout; rm -rf /opt/pdk/t/$X": "names /opt/pdk/t",
    "RUN cd /tmp && [ \"$O\" 'a[$(rm -rf /opt/pdk/t/klayout)]'\"$N\" ]": "subscript",
    "RUN X=/opt/pdk/t/klayout; \"for\" X in /tmp/safe; rm -rf $X": "has for",
    "RUN fd=a/b; : {fd[0]}>/dev/null; cd /opt/pdk/t/klayout/$fd/.. && rm -f t.lyt": "{fd[0]}>",
    "RUN fd=a/b; : {fd[\" 0\"]}>/dev/null; cd /opt/pdk/t/klayout/$fd/.. && rm -f t.lyt":
        "variable to set",
    "RUN set -- x; for X do rm -f /opt/pdk/t/klayout/t.lyt; done": "without in",
    "RUN TAR_OPTIONS+=-C/opt/pdk/t tar -xf /tmp/a.tar": "TAR_OPTIONS",
    "RUN V='-v a[$(rm${IFS}-rf${IFS}/opt/pdk/t/klayout)]'; [ $V ] || true": "subscript",
    "RUN set -- -v 'a[$(rm -rf /opt/pdk/t/klayout)]'; [ \"$@\" ] || true": "subscript",
}
missing = set(REASONS) - set(REFUSED)
assert not missing, missing
# And each of these leaves them.
PASSED = [
    "RUN test -f %s && mkdir -p /opt/pdk/t/x && ln -sT /opt/x /opt/other"
    " && [ \"$(find /opt/pdk -type f | sort)\" ]" % LYT,
    "RUN echo x > /tmp/y 2>/dev/null && echo y >&2",
    "RUN cp -L %s /opt/pdk/t/klayout/t.lyt.orig /tmp/ && cp -t /tmp /opt/pdk/t" % LYT,
    "RUN ln -sT /opt/pdk/t /opt/alias && ln -sT \"$X\" /opt/cosim && rm -rf /opt/elsewhere",
    "RUN cd /opt/pdk/t && mkdir y && rm -rf /opt/pdk/t/klayout/x",
    "RUN cd \"$X\"; cd /tmp && rm -rf klayout",
    "RUN pip install \"klayout==${NOT_SET}\" && python3 -m venv /opt/venv",
    "WORKDIR /opt/pdk/t/klayout\nRUN ls && cat t.lyt > /tmp/copy",
    "RUN for f in a b; do cp \"/src/$f\" /opt/other/; done",
    "RUN { echo \"bad $pin\" >&2; exit 1; } || true",
    "RUN true || { rc=$?; { echo a; exit \"$rc\"; }; }",
    "RUN chmod -R a-w /opt/pdk && chmod -R go+rX,u-w /opt/pdk",
    "RUN sh /run/apt-snapshot/apt-snapshot.sh \"$T\" $(dpkg-query -W -f='${binary:Package}\\n')",
    "COPY --from=studio /opt/s/stackups/ /opt/s/stackups/",
    'RUN for tree in a b; do git config --system --add safe.directory "/opt/staging/$tree"; done',
    "RUN command -v git && git -C /tmp/r status && cd /tmp && rm -rf klayout",
    "WORKDIR /tmp\nRUN cp -t \"\" /opt/pdk/t/klayout/t.lyt /tmp/x",
    "RUN E=; P=/tmp $E; cd $P && rm -rf klayout && sed -i -e p /tmp/t.lyt",
    "RUN while read -r name want; do echo \"$name $want\"; done < /tmp/list",
    "RUN true && [ \"$(cat /tmp/a)\" = \"$(cat /tmp/b)\" ] && test -v X",
    "ARG TARGETARCH\nRUN if [ $TARGETARCH = amd64 ]; then test -f $(command -v ls); fi",
    "RUN for X in a b; do echo $X; done; for Y; do echo $Y; done",
    "RUN echo {a,b}>/tmp/a && [ $(id -u) -eq 0 ]",
]


def extra(entry):
    return (entry, "") if isinstance(entry, str) else entry


@case("a step that may change a checked file is refused, never passed, in / and in /work")
def _():
    leaked = []
    with tempfile.TemporaryDirectory() as tmp:
        check = scratch(tmp)
        for entry in REFUSED:
            tools, pdk = extra(entry)
            for workdir in ("", "WORKDIR /work\n"):
                _, _, failures = check(BOTH, workdir + tools + "\n", pdk)
                if not failures or not all("cannot read the image's" in f for f in failures) \
                        or not any(REASONS.get(tools, "") in f for f in failures):
                    leaked.append((workdir + tools, failures))
    for entry, failures in leaked:
        print("   passed or failed otherwise: %r -> %s" % (entry, failures))
    return not leaked


@case("a step that leaves the checked files passes")
def _():
    refused = []
    with tempfile.TemporaryDirectory() as tmp:
        check = scratch(tmp)
        for entry in PASSED:
            tools, pdk = extra(entry)
            _, _, failures = check(BOTH, tools + "\n", pdk)
            if failures:
                refused.append((entry, failures))
    for entry, failures in refused:
        print("   refused: %r -> %s" % (entry, failures))
    return not refused


@case("a checkout stated in a way this reader does not model is refused")
def _():
    out = []
    for paths, why in ((BOTH + ["':!t/klayout/t.lyt'"], "pathspec"),
                       (BOTH + ["&& git -C /pdk fetch -q origin %s" % ("0" * 40)], "fetches twice"),
                       (BOTH + ["&& umask 077"], "umask"),
                       (BOTH + ["&& cd /pdk && git fetch -q origin %s" % ("0" * 40)],
                        "fetches twice"),
                       (BOTH + ["&& git -C /pdk -c a.b=c log"], "gives git -c"),
                       (BOTH + ["&& git -C /pdk reset -q --hard"], "does not model"),
                       (BOTH + ["'t/klayout/*.lyt'"], "pathspec"),
                       (BOTH + ["&& GIT_DIR=/pdk/.git git fetch -q origin %s" % ("0" * 40)],
                        "GIT_DIR"),
                       (BOTH + ["&& git config --global core.hooksPath /tmp/h"],
                        "sets git config"),
                       (BOTH + ["&& git clone -q /tmp/r /pdk/sub && rm -rf /pdk/sub/.git"
                                " && git -C /pdk/sub fetch -q origin %s" % ("0" * 40)],
                        "does not model")):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, failures = world(tmp, paths)
        out.append(bool(failures) and all(why in f for f in failures))
    # A repository cloned inside the checkout is its own, as the real pdk
    # stage clones pycell4klayout-api under /pdk.
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH + ["&& git clone -q /tmp/r /pdk/t/sub"
                                            " && git -C /pdk/t/sub checkout -q x"])
    return all(out) and not failures


@case("KNOWN waives its one command of its one stage, outside what it writes")
def _():
    key = ("runtime", "rm -rf /opt/pdk/t")
    outside, over = {key: (("/opt/pdk/t/klayout/x",), "a test")}, {key: (("/opt/pdk/t",), "a test")}
    with tempfile.TemporaryDirectory() as tmp:
        _, _, alone = world(tmp, BOTH, "RUN rm -rf /opt/pdk/t\n", known=outside)
    with tempfile.TemporaryDirectory() as tmp:
        _, _, more = world(tmp, BOTH, "RUN rm -rf /opt/pdk/t && rm -rf /opt/pdk\n", known=outside)
    with tempfile.TemporaryDirectory() as tmp:
        _, _, inside = world(tmp, BOTH, "RUN rm -rf /opt/pdk/t\n", known=over)
    return (not alone and more and all("'rm -rf /opt/pdk'" in f for f in more)
            and inside and all("'rm -rf /opt/pdk/t'" in f for f in inside))


@case("a .lyt that is a symbolic link in its source is refused")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, lyt_link=True)
    return failures and all("symbolic link" in f for f in failures)


@case("an absolute technology path is refused, never read from this host")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, sheet_root="/opt/pdk")
    return failures == ["t layer_properties /opt/pdk/t/klayout/t.lyp is absolute; this "
                        "check reads only paths under an image root (${VAR})"]


@case("a checkout at a branch, not a sha, is refused")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, ref="HEAD")
    return failures and all("not a commit sha" in f for f in failures)


@case("a checkout at a sha the clone lacks is refused, never fetched")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, ref="0" * 40)
    return failures and all("never fetches" in f for f in failures)


@case("a file .dockerignore excludes is not in the image")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, dockerignore="**/stackups\n")
    with tempfile.TemporaryDirectory() as tmp:
        _, _, later = world(tmp, BOTH, dockerignore="!studio/stackups/t.yaml\n**/stackups\n")
    with tempfile.TemporaryDirectory() as tmp:
        _, _, kept = world(tmp, BOTH, dockerignore="**/stackups\n!studio/stackups/t.yaml\n")
    return (len(failures) == 1 and "t stackup" in failures[0] and "STUDIO_ROOT" in failures[0]
            and later == failures and not kept)


@case("a COPY through a symbolic link in the build context is refused")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, failures = world(tmp, BOTH, extra_tools="COPY ovl/ /opt/pdk/t/\n",
                               context={"real/klayout/t.lyt": "<x/>\n"},
                               links=[("ovl", "real")])
    return failures and all("ovl is a symbolic link in the build context" in f
                            for f in failures)


def main():
    failed = []
    for name, fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001 - a crashing case is a failed case
            ok = False
            name = "%s (raised %s: %s)" % (name, type(exc).__name__, exc)
        if not ok:
            failed.append(name)
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
