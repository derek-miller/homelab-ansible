#!/usr/bin/env python3
"""Tests for root's SSH on pve1-5: passwords off through a drop-in, a key file
off /etc/pve, rpcbind masked, and a bootstrap that writes authorized_keys
through PVE's link. Read as text, since CI's venv holds just yamllint."""

import os
import re
import sys

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DROP_IN = "/etc/ssh/sshd_config.d/10-ansible-managed.conf"

results = []


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("      got:  %r" % (got,))
        print("      want: %r" % (want,))
    results.append(ok)
    return ok


def read(*parts):
    with open(os.path.join(ROLE, *parts)) as f:
        return f.read()


def tasks(text):
    """Each `- name:` task with its body, nested ones included."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)- name: (.*)$", line)
        if not m:
            continue
        indent = len(m.group(1))
        body = [line]
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        out.append((m.group(2).strip(), "\n".join(body)))
    return out


def task(text, name):
    found = [body for n, body in tasks(text) if n == name]
    return found[0] if len(found) == 1 else ""


def names(text):
    return [n for n, _ in tasks(text)]


NODE = read("tasks", "node.yml")
ABSENT = read("tasks", "absent.yml")
HANDLERS = read("handlers", "main.yml")
BOOTSTRAP = read("tasks", "bootstrap.yml")


def test_drop_in():
    t = task(NODE, "refuse ssh passwords")
    check("the drop-in goes to sshd_config.d", "dest: %s" % DROP_IN in t, True)
    content = re.search(r"content: \|\n((?:\s{6}.*\n)+)", t + "\n")
    lines = [l.strip() for l in content.group(1).splitlines()] if content else []
    check(
        "the drop-in holds exactly the key-only directives",
        lines,
        [
            "PermitRootLogin prohibit-password",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "AuthorizedKeysFile .ssh/authorized_keys2 .ssh/authorized_keys",
        ],
    )
    check("sshd checks the drop-in before it lands", "validate: /usr/sbin/sshd -t -f %s" in t, True)
    check("the drop-in reloads sshd", "notify: reload sshd" in t, True)
    check("node.yml writes one sshd drop-in", len(re.findall(r"/etc/ssh/sshd_config\.d/\S+", NODE)), 1)


def test_handler():
    t = task(HANDLERS, "reload sshd")
    check("the handler reloads ssh.service", ("name: ssh\n" in t, "state: reloaded" in t), (True, True))
    check("the handler listens for reload sshd", "listen: reload sshd" in t, True)


def test_local_keys():
    t = task(NODE, "keep root's ssh keys on the node's own disk")
    check("the local key file is authorized_keys2", "dest: /root/.ssh/authorized_keys2" in t, True)
    check("it holds the ansible key", "lookup('file', ansible_surrogate_public_key_file)" in t, True)
    check("it holds proxmox_root_ssh_keys", "proxmox_root_ssh_keys" in t, True)
    check("it is root's alone", ("owner: root" in t, "group: root" in t, "mode: 0600" in t), (True, True, True))
    order = names(NODE)
    check(
        "the local keys land before passwords turn off",
        order.index("keep root's ssh keys on the node's own disk") < order.index("refuse ssh passwords"),
        True,
    )


def test_rpcbind():
    t = task(NODE, "stop and mask rpcbind")
    check("rpcbind is masked and stopped", ("masked: yes" in t, "state: stopped" in t), (True, True))
    check(
        "the socket is masked before the service",
        re.findall(r"- (rpcbind\.\w+)", t),
        ["rpcbind.socket", "rpcbind.service"],
    )


def test_absent_reverses_present():
    t = task(ABSENT, "allow ssh passwords again")
    check("absent removes the same drop-in", ("path: %s" % DROP_IN in t, "state: absent" in t), (True, True))
    check("absent reloads sshd", "notify: reload sshd" in t, True)
    t = task(ABSENT, "remove root's ssh keys from the node's own disk")
    check("absent removes the local key file", "path: /root/.ssh/authorized_keys2" in t, True)
    t = task(ABSENT, "unmask and start rpcbind")
    check("absent unmasks and starts rpcbind", ("masked: no" in t, "state: started" in t), (True, True))
    check(
        "absent unmasks the same units",
        re.findall(r"- (rpcbind\.\w+)", t),
        re.findall(r"- (rpcbind\.\w+)", task(NODE, "stop and mask rpcbind")),
    )


def test_bootstrap_writes_through_the_link():
    check(
        "bootstrap uses no authorized_key module",
        re.findall(r"(?m)^\s*(?:ansible\.posix\.)?authorized_key:", BOOTSTRAP),
        [],
    )
    writes = [
        (n, b) for n, b in tasks(BOOTSTRAP)
        if "/root/.ssh/authorized_keys" in b and "block:" not in b
    ]
    check("both key tasks name root's authorized_keys", len(writes), 2)
    for n, b in writes:
        check("%s uses lineinfile" % n, "lineinfile:" in b, True)
        check(
            "%s sets no owner, group or mode" % n,
            re.findall(r"(?m)^\s*(owner|group|mode):", b),
            [],
        )
    check(
        "the slurped key loses its newline",
        "proxmox_root_pubkey.content | b64decode | trim" in BOOTSTRAP,
        True,
    )


for fn in (
    test_drop_in,
    test_handler,
    test_local_keys,
    test_rpcbind,
    test_absent_reverses_present,
    test_bootstrap_writes_through_the_link,
):
    fn()

failed = results.count(False)
print("\n%d passed, %d failed" % (results.count(True), failed))
sys.exit(1 if failed else 0)
