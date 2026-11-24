#!/usr/bin/env python3
"""Tests for the provisioning API token: it is narrowed to its own grants
with an expiry, those grants are only what proxmox-guest needs, and absent
takes them away before the role they name. Read as text, since CI's venv
holds just yamllint."""

import os
import re
import sys

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
GROUP_VARS = os.path.join(ROLE, "..", "..", "..", "group_vars", "proxmox", "vars.yml")

results = []


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("      got:  %r" % (got,))
        print("      want: %r" % (want,))
    results.append(ok)
    return ok


def read(path):
    with open(path) as f:
        return f.read()


def role(*parts):
    return read(os.path.join(ROLE, *parts))


def names(text):
    return [m.strip() for m in re.findall(r"(?m)^\s*- name: (.*)$", text)]


def task(text, name):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)- name: (.*)$", line)
        if m and m.group(2).strip() == name:
            body = [line]
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= len(m.group(1)):
                    break
                body.append(nxt)
            return "\n".join(body)
    return ""


CLUSTER = role("tasks", "cluster.yml")
ABSENT = role("tasks", "absent.yml")
DEFAULTS = role("defaults", "main.yml")
VARS = read(GROUP_VARS)


def test_the_token_is_narrowed():
    check("nothing asks for a privsep 0 token", "--privsep 0" in CLUSTER, False)
    check("a missing token is made with privsep 1",
          bool(re.search(r"pveum user token add .*--privsep 1", task(CLUSTER, "require the provisioning api token"))),
          True)
    t = task(CLUSTER, "narrow the provisioning api token")
    check("the token is set to privsep 1 with an expiry", ("--privsep 1" in t, "--expire" in t), (True, True))
    check("the lifetime is read as a number in both places",
          t.count("(proxmox_api_token_lifetime_days | int) * 86400"), 2)

    order = names(CLUSTER)
    sequence = [
        "require the provisioning api token",
        "add roles",
        "converge roles",
        "grant the provisioning token's acls",
        "narrow the provisioning api token",
    ]
    check("the token is checked, then given its role and grants, then narrowed",
          [order.index(n) for n in sequence], sorted(order.index(n) for n in sequence))


def test_the_grants():
    t = task(CLUSTER, "grant the provisioning token's acls")
    check("token grants go to the token", "--tokens {{ proxmox_api_user }}!{{ proxmox_api_token_id }}" in t, True)
    check("token grants don't propagate", "--propagate 0" in t, True)
    check("token grants loop over proxmox_api_token_acls", 'loop: "{{ proxmox_api_token_acls }}"' in t, True)
    check("user grants stay on proxmox_acls alone",
          'loop: "{{ proxmox_acls }}"' in task(CLUSTER, "grant acls"), True)
    for name in ("grant the provisioning token's acls", "narrow the provisioning api token"):
        check("%s runs without a shell, so ! passes through" % name, "shell:" in task(CLUSTER, name), False)

    privs = re.search(r"roleid: Provisioner\n\s+privs: \[(.*?)\]", VARS)
    privs = sorted(p.strip() for p in privs.group(1).split(",")) if privs else []
    check("Provisioner holds just what the clone and config calls need", privs, sorted([
        "VM.Allocate", "VM.Audit", "VM.Config.CPU", "VM.Config.Cloudinit", "VM.Config.Memory", "VM.Config.Options",
    ]))
    check("Provisioner reaches no console, power, agent, disk or network privilege",
          [p for p in privs if re.search(r"GuestAgent|Console|PowerMgmt|Migrate|Snapshot|Config\.(Disk|Network|HWType)", p)],
          [])


def test_defaults():
    for line in ("proxmox_roles: []", "proxmox_api_token_acls: []"):
        check("defaults declare %s" % line, line in DEFAULTS, True)
    check("defaults declare a token lifetime", bool(re.search(r"(?m)^proxmox_api_token_lifetime_days: \d+$", DEFAULTS)), True)


def test_absent():
    block = task(ABSENT, "provisioning token access")
    check("absent strips token access only in the cluster scope", "when: proxmox_scope == 'cluster'" in block, True)
    order = names(block)
    check("the token's grants go before the roles they name",
          order.index("revoke the provisioning token's acls") < order.index("remove roles"), True)


for fn in (
    test_the_token_is_narrowed,
    test_the_grants,
    test_defaults,
    test_absent,
):
    fn()

failed = results.count(False)
print("\n%d passed, %d failed" % (results.count(True), failed))
sys.exit(1 if failed else 0)
