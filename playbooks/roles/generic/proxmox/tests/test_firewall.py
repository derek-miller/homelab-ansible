#!/usr/bin/env python3
"""Tests for the pve firewall files: what cluster.fw and host.fw must always
say, where the role writes them, and the order absent removes them in. Read
as text, since CI's venv holds just yamllint."""

import os
import re
import sys

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PLAYBOOK = os.path.join(ROLE, "..", "..", "..", "proxmox.yml")

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


def strip_comments(template):
    return re.sub(r"\{#.*?#\}", "", template, flags=re.S)


CLUSTER_FW = role("templates", "pve", "cluster.fw.jinja2")
HOST_FW = role("templates", "pve", "host.fw.jinja2")
CLUSTER = role("tasks", "cluster.yml")
NODE = role("tasks", "node.yml")
ABSENT = role("tasks", "absent.yml")
FW_ABSENT = role("tasks", "firewall-absent.yml")
PRESENT = role("tasks", "present.yml")


def test_templates_hold_no_addresses():
    for name, text in (("cluster.fw", CLUSTER_FW), ("host.fw", HOST_FW)):
        check("%s has no IPv4 literal" % name, re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), [])
        check("%s ends with a newline" % name, text.endswith("\n"), True)


def test_cluster_fw():
    body = strip_comments(CLUSTER_FW)
    check("the switch follows proxmox_firewall_enabled",
          "enable: {{ proxmox_firewall_enabled | bool | ternary(1, 0) }}" in body, True)
    check("inbound is dropped unless a rule allows it", "policy_in: DROP" in body, True)
    check("outbound stays open", "policy_out: ACCEPT" in body, True)
    check("no local_network alias replaces PVE's detected LAN", "local_network" in body, False)
    check("the fabric alias is the migration network", "fabric {{ proxmox_migration_network }}" in body, True)

    ipset = re.search(r"\[IPSET management\]\n(.*?)\n\[", body, re.S)
    entries = ipset.group(1) if ipset else ""
    check("the management set holds the fabric", "dc/fabric" in entries, True)
    check("the management set holds proxmox_firewall_management", "proxmox_firewall_management" in entries, True)

    rule = re.search(r"(?m)^IN ACCEPT -source dc/fabric -p udp -dport (\d+):(\d+)$", body)
    check("corosync's second link is open on the fabric",
          bool(rule) and int(rule.group(1)) <= 5406 <= int(rule.group(2)), True)
    check("peers in the management set can ping", "IN Ping(ACCEPT) -source +dc/management" in body, True)


def test_host_fw():
    body = strip_comments(HOST_FW)
    check("the node's filter follows proxmox_firewall_host_enabled",
          "enable: {{ proxmox_firewall_host_enabled | bool | ternary(1, 0) }}" in body, True)
    check("conntrack never drops guest packets it can't classify",
          re.findall(r"(?m)^nf_conntrack_allow_invalid: .*$", body), ["nf_conntrack_allow_invalid: 1"])
    check("the nftables backend, which the RUNBOOK's recovery steps don't cover, stays off",
          re.findall(r"(?m)^nftables:.*$", body), [])


def test_where_the_files_are_written():
    t = task(NODE, "configure the host firewall")
    check("node.yml writes this node's host.fw",
          'dest: "/etc/pve/nodes/{{ inventory_hostname }}/host.fw"' in t, True)
    t = task(CLUSTER, "configure the datacenter firewall")
    check("cluster.yml writes cluster.fw", "dest: /etc/pve/firewall/cluster.fw" in t, True)

    order = names(CLUSTER)
    check("the firewall folder is made before cluster.fw",
          order.index("create the firewall directory") < order.index("configure the datacenter firewall"), True)

    plays = names(read(PLAYBOOK))
    check("every node's host.fw is written before cluster.fw in a full run",
          plays.index("proxmox nodes") < plays.index("proxmox cluster"), True)
    check("cluster.fw needs the migration network", "- proxmox_migration_network" in PRESENT, True)


def test_absent_order():
    check("absent includes the firewall removal", "- include_tasks: firewall-absent.yml" in ABSENT, True)
    order = names(FW_ABSENT)
    check("cluster.fw goes before host.fw",
          order.index("remove the datacenter firewall") < order.index("remove the host firewall"), True)
    check("cluster.fw is removed once for the cluster",
          "run_once: true" in task(FW_ABSENT, "remove the datacenter firewall"), True)


for fn in (
    test_templates_hold_no_addresses,
    test_cluster_fw,
    test_host_fw,
    test_where_the_files_are_written,
    test_absent_order,
):
    fn()

failed = results.count(False)
print("\n%d passed, %d failed" % (results.count(True), failed))
sys.exit(1 if failed else 0)
