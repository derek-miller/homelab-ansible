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
GUESTS = os.path.join(ROLE, "..", "proxmox-guest", "tasks", "guests.yml")

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
GUEST_TASKS = read(GUESTS)
TOKEN = "proxmox_api_user ~ '!' ~ proxmox_api_token_id"


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
        "revoke the provisioning token's other acls",
        "narrow the provisioning api token",
    ]
    check("the token is checked, then given its role and grants, stripped of others, then narrowed",
          [order.index(n) for n in sequence], sorted(order.index(n) for n in sequence))


def test_the_grants():
    t = task(CLUSTER, "grant the provisioning token's acls")
    check("token grants go to the token", "--tokens {{ proxmox_api_user }}!{{ proxmox_api_token_id }}" in t, True)
    check("token grants don't propagate", "--propagate 0" in t, True)
    check("token grants loop over proxmox_api_token_acls", 'loop: "{{ proxmox_api_token_acls }}"' in t, True)
    check("a grant left propagating is set again", "selectattr('propagate', 'equalto', 0)" in t, True)
    check("user grants stay on proxmox_acls alone",
          'loop: "{{ proxmox_acls }}"' in task(CLUSTER, "grant acls"), True)
    for name in ("grant the provisioning token's acls", "revoke the provisioning token's other acls",
                 "narrow the provisioning api token"):
        check("%s runs through command:" % name, "shell:" in task(CLUSTER, name), False)

    t = task(CLUSTER, "revoke the provisioning token's other acls")
    check("present revokes only this token's grants",
          ("selectattr('type', 'equalto', 'token')" in t, "selectattr('ugid', 'equalto', %s)" % TOKEN in t),
          (True, True))
    keep = re.search(r"when: >-\n((?:\s{4}.*\n?)+)", t + "\n")
    keep = re.sub(r"\s+", " ", keep.group(1)).strip() if keep else ""
    check("present keeps exactly the path and role pairs proxmox_api_token_acls declares", keep,
          "(proxmox_api_token_acls | selectattr('path', 'equalto', item.path)"
          " | selectattr('roleid', 'equalto', item.roleid) | list | length) == 0")
    check("present revokes with pveum acl delete", "pveum acl delete" in t, True)

    privs = re.search(r"roleid: Provisioner\n\s+privs: \[(.*?)\]", VARS)
    privs = sorted(p.strip() for p in privs.group(1).split(",")) if privs else []
    check("Provisioner holds just what the clone and config calls need", privs, sorted([
        "VM.Allocate", "VM.Audit", "VM.Config.CPU", "VM.Config.Cloudinit", "VM.Config.Memory", "VM.Config.Options",
    ]))
    check("Provisioner reaches no console, power, agent, disk or network privilege",
          [p for p in privs if re.search(r"GuestAgent|Console|PowerMgmt|Migrate|Snapshot|Config\.(Disk|Network|HWType)", p)],
          [])


def test_the_grant_set():
    m = re.search(r"(?ms)^proxmox_api_token_acls: >-\n(.*?)^\S", VARS)
    acls = m.group(1) if m else ""
    for path, role in (
        ("'/storage/' ~ proxmox_guest_storage", "PVEDatastoreUser"),
        ("'/sdn/zones/localnetwork/vmbr0'", "PVESDNUser"),
        ("'/vms/' ~ hostvars[node].proxmox_guest_template_vmid", "PVETemplateUser"),
        ("'/vms/' ~ g.vmid", "Provisioner"),
    ):
        check("the token gets %s on %s" % (role, path),
              bool(re.search(re.escape("'path': " + path) + r",\s*'roleid': '" + role + "'", acls)), True)
    check("templates are granted for each node with a guest",
          "guests | map(attribute='node') | unique" in acls, True)
    check("guests are granted one by one", "{%- for g in guests -%}" in acls, True)
    check("a throwaway proxmox_guests adds to the inventory's grants rather than replacing them",
          bool(re.search(r"set guests = proxmox_inventory_guests\s*\+\s*\(proxmox_guests\s*\|\s*rejectattr", acls)), True)
    check("proxmox_guests defaults to the inventory's guests",
          'proxmox_guests: "{{ proxmox_inventory_guests }}"' in VARS, True)


def test_guest_runs_skip_existing_guests():
    t = task(GUEST_TASKS, "clone guests from the template")
    check("only a guest missing from the cluster is cloned, so a rebuilt template's lost grant can't fail existing guests",
          "item.vmid | int not in (proxmox_guest_cluster_vms.stdout | from_json | map(attribute='vmid') | list)" in t,
          True)
    order = names(GUEST_TASKS)
    check("the cluster's guests are read before the clone",
          order.index("read the cluster's guests") < order.index("clone guests from the template"), True)


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
    test_the_grant_set,
    test_guest_runs_skip_existing_guests,
    test_defaults,
    test_absent,
):
    fn()

failed = results.count(False)
print("\n%d passed, %d failed" % (results.count(True), failed))
sys.exit(1 if failed else 0)
