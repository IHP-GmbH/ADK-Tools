#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""ci/leave_policy.py: whether a command, read by ci/shell_reader.py, leaves a
path, row by row.

Usage: python3 -B ci/test_leave_policy.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leave_policy import aliases, leaves  # noqa: E402
from shell_reader import Shell  # noqa: E402


def main():
    """Each row: a RUN's text, the directory it runs in, and whether every
    command of it leaves FILE (put there by a step that wrote TOP)."""
    FILE, TOP = "/opt/pdk/t/klayout/t.lyt", "/opt/pdk"
    rows = [
        ("cp -L %s /opt/pdk/t/klayout/x /tmp/ && cp -t /tmp /opt/pdk/t" % FILE, "/", True),
        ("cp /x /opt/pdk/t/klayout/", "/", False),
        ("cp -t /opt/pdk /z", "/", False),
        ("cp -rt /opt/pdk/t /tmp/klayout", "/", False),
        ("cp -t/opt/pdk/t/klayout /tmp/t.lyt", "/", False),
        ("cp --target /opt/pdk/t/klayout /tmp/t.lyt", "/", False),
        ("cp -l %s /tmp/h" % FILE, "/", False),
        ("cp $X /tmp/y", "/", False),
        ("ln -s /x $X /tmp/y", "/", False),
        ("cp --backup --suffix=t /y /opt/pdk/t/klayout/t.ly", "/", False),
        ("/bin/cp /x %s" % FILE, "/", False),
        ("chmod -R a-w /opt/pdk && chmod -R go+rX,u-w /opt/pdk", "/", True),
        ("chmod 600 %s" % FILE, "/", False),
        ("chmod -R go-x /opt/pdk", "/", False),
        ("git -C /opt/pdk log -1 && git config --global --get a.b", "/", True),
        ("git config --global core.hooksPath /tmp/h", "/", False),
        ("git init -q --template=/tmp/tpl /tmp/r", "/", False),
        ("git -C /tmp/r checkout -q x", "/", False),
        ("git -C /tmp/r status", "/", True),
        ("git -C /opt/pdk/t/sub checkout -q x", "/", False),
        ("git checkout -q x", "/opt/pdk/t/sub", False),
        ("GIT_DIR=/opt/pdk/.git git -C /tmp/r fetch -q origin x", "/", False),
        ("export GIT_WORK_TREE=/opt/pdk/t && git -C /tmp/r checkout -q x", "/", False),
        ("git --git-dir=/tmp/r/.git -c core.worktree=/opt/pdk checkout -f HEAD -- t", "/tmp",
         False),
        ('git config --system --add safe.directory "/opt/staging/$tree"', "/", True),
        ("git -C /tmp log --output=%s -1" % FILE, "/", False),
        ("git -C /opt/pdk checkout -q HEAD -- t", "/", False),
        ("git clone -q /x /opt/other", "/opt/pdk", True),
        ("git clone -q --depth 1 --branch v1 /x /opt/other", "/", True),
        ("git clone -q /x /opt/pdk/t", "/", False),
        ("git clone -u 'rm -rf /opt/pdk' /x /opt/other", "/", False),
        ("git -C /r fetch --upload-pack='rm -rf /opt/pdk' origin", "/", False),
        ("git -C /tmp -c core.worktree=/opt/pdk checkout -f HEAD -- t", "/", False),
        ("git config --fil %s a.b c" % FILE, "/", False),
        ("GIT_WORK_TREE=/opt/pdk/t git --git-dir=/r/.git checkout -- .", "/work", False),
        ("GIT_CONFIG=%s git config a.b c" % FILE, "/", False),
        ("find /opt/pdk -type f -name '*.lyt'", "/", True),
        ("find /opt/pdk -delete", "/", False),
        ("find /tmp -exec sh -c x \\;", "/", False),
        ("find /tmp -exec /bin/sh -c x \\;", "/", False),
        ("find /tmp -exec env sh -c x \\;", "/", False),
        ("ln -s %s /tmp/l" % FILE, "/", True),
        ("ln -s /x %s" % FILE, "/", False),
        ("ln -s /x \"$X\"", "/", False),
        ("ln --frob /x /tmp/y", "/", False),
        ("dd if=/dev/null of=%s" % FILE, "/", False),
        ("echo x > /tmp/y 2>/dev/null && echo y >&2", "/", True),
        ("echo x >> %s" % FILE, "/", False),
        ("rm -rf /tmp/$X", "/", False),
        ('rm -rf "/tmp/$X"', "/", True),
        ("for f in 'a /opt/pdk/t'; do rm -rf /tmp/$f; done", "/", False),
        ("command cd /opt/pdk/t/klayout && rm -f t.lyt", "/", False),
        ("command -v cd /opt/pdk/t/klayout && rm -f t.lyt", "/", True),
        ("eval 'cd /opt/pdk/t' && rm -rf klayout", "/", False),
        ("set -e; (cd /nonexistent; rm -f t.lyt) || true", "/opt/pdk/t/klayout", False),
        ("rm -rf /opt/pdk/t$X", "/", False),
        ("rm -rf klayout", "/opt/pdk/t", False),
        ("ls && cat t.lyt > /tmp/copy", "/opt/pdk/t/klayout", True),
        ("make", "/opt/pdk", False),
        ("python3 x.py", "/opt", True),
        ("python3 x.py", "/opt/pdk/t", False),
        ("HOME=\"$home\" python3 x.py && DESTDIR=/install ninja install", "/tmp", True),
        ("DESTDIR=/opt ninja install", "/tmp", False),
        ("$CMD /x", "/", False),
        ("mkdir -p /opt/pdk/t/x && touch %s" % FILE, "/", True),
        ("git -C /tmp/r archive -o ../../opt/pdk/t/klayout/t.lyt HEAD", "/w/x/y", False),
        ("git -C /tmp/r checkout -q x -- ':/opt/pdk/t'", "/", False),
        ("cp {-t/opt/pdk/t/klayout,/tmp/a} /tmp/b", "/", False),
        ("cp /tmp/{a,b} /tmp/c", "/", False),
        ("cp /tmp/{} /tmp/c", "/", True),
        ("P=/tmp/x; P=/opt/pdk/t sh -c 'rm -rf \"$P\"'", "/", False),
        ("cd /tmp || cd /opt/pdk/t/klayout || exit 1; rm -f t.lyt", "/", False),
        ("cd /tmp || exit 1; rm -rf klayout", "/", False),
        ("set -e; cd /tmp; rm -rf klayout", "/", True),
    ]
    failed = []
    for text, cwd, left in rows:
        whys = [leaves(c, FILE, TOP) for c in Shell(lambda n: None).commands(text, cwd)]
        if (not any(whys)) != left:
            failed.append((text, cwd, whys))
    for text, cwd, whys in failed:
        print("FAIL: %r in %s -> %s" % (text, cwd, whys))
    made = [leaves(c, FILE, FILE, held=False) for c in Shell(lambda n: None).commands(
        "mkdir -p /opt/pdk/t/klayout && touch %s" % FILE, "/")]
    if made[0] or not made[1]:
        print("FAIL: making the file where it is not held -> %s" % made)

    def links(text, cwd="/"):
        return aliases(next(Shell(lambda n: None).commands(text, cwd)), FILE)[:2]
    got = [links("ln -sT /opt/pdk/t /opt/alias"), links("ln -sT \"$X\" /opt/w"),
           links("ln -sr pdk /tmp/l", "/opt"), links("ln -st/tmp /opt/pdk/t")]
    linked = got == [(["/opt/alias/klayout/t.lyt"], []), ([], ["/opt/w"]),
                     (["/tmp/l/t/klayout/t.lyt", "/tmp/l/pdk/t/klayout/t.lyt"], []),
                     (["/tmp/t/klayout/t.lyt"], [])]
    if not linked:
        print("FAIL: aliases %s" % got)
    print("%d/%d rows leave or refuse as stated, making %s, links %s"
          % (len(rows) - len(failed), len(rows), "refused" if made[1] else "MISREAD",
             "named" if linked else "MISREAD"))
    return 1 if failed or not linked or made[0] or not made[1] else 0


if __name__ == "__main__":
    sys.exit(main())
