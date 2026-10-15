#!/usr/bin/env python3
"""Remove the runtime state k3s leaves on a host, and nothing else.

Usage: k3s-teardown.py DATA_DIR

The stock k3s-killall.sh also clears Tailscale's advertised routes and pipes
all of iptables through grep into iptables-restore, so it is never used here.
Prints {"changed": bool, "errors": [...]} and exits 1 if anything failed.
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

DATA_DIR = None
OUR_CHAIN = re.compile(r'^(KUBE-|CNI-|FLANNEL-)')
OUR_RULE = re.compile(r'KUBE-|CNI-|flannel', re.IGNORECASE)
OUR_NFT_TABLE = re.compile(r'^(kube-|flannel)')
OUR_IPSET = re.compile(r'^(KUBE-|kube-router)')
LINKS = ('cni0', 'flannel.1', 'flannel-v6.1', 'flannel-wg', 'flannel-wg-v6', 'kube-ipvs0')

changed = False
errors = []


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def must(*cmd):
    global changed
    result = run(*cmd)
    if result.returncode == 0:
        changed = True
    else:
        errors.append(f"{shlex.join(cmd)}: {result.stderr.strip()}")
    return result


def processes():
    table = {}
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/stat') as f:
                ppid = int(f.read().rsplit(')', 1)[1].split()[1])
            with open(f'/proc/{entry}/cmdline', 'rb') as f:
                argv0 = f.read().split(b'\0')[0].decode(errors='replace')
        except (OSError, IndexError, ValueError):
            continue
        table[int(entry)] = (ppid, argv0)
    return table


def kill_pods():
    """SIGKILL every k3s containerd shim and everything below it."""
    global changed
    table = processes()
    children = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    prefix = DATA_DIR + '/data/'
    stack = [pid for pid, (_, argv0) in table.items()
             if argv0.startswith(prefix) and '/bin/containerd-shim' in argv0]
    victims = set()
    while stack:
        pid = stack.pop()
        if pid not in victims:
            victims.add(pid)
            stack.extend(children.get(pid, []))
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
            changed = True
        except ProcessLookupError:
            pass
    for _ in range(20):
        if not victims & set(processes()):
            return
        time.sleep(0.5)
    errors.append(f"processes still alive after SIGKILL: {sorted(victims & set(processes()))}")


def unmount_under(prefix):
    global changed
    with open('/proc/self/mounts') as f:
        points = [line.split()[1].encode().decode('unicode_escape') for line in f]
    for point in sorted((p for p in points if p.startswith(prefix)), reverse=True):
        if run('umount', '-f', point).returncode == 0:
            changed = True
        else:
            must('umount', '-l', point)


def remove_netns():
    for line in run('ip', 'netns', 'list').stdout.splitlines():
        name = line.split()[0] if line.split() else ''
        if name.startswith('cni-'):
            must('ip', 'netns', 'delete', name)


def remove_links():
    for line in run('ip', '-o', 'link', 'show', 'master', 'cni0').stdout.splitlines():
        name = line.split(':')[1].strip().split('@')[0]
        must('ip', 'link', 'delete', name)
    for name in LINKS:
        if run('ip', 'link', 'show', name).returncode == 0:
            must('ip', 'link', 'delete', name)


def remove_iptables(binary):
    if not shutil.which(binary) or not shutil.which(binary + '-save'):
        return
    table, chains, rules = None, {}, []
    for line in run(binary + '-save').stdout.splitlines():
        if line.startswith('*'):
            table = line[1:]
            chains[table] = []
        elif line.startswith(':') and table:
            chain = line[1:].split()[0]
            if OUR_CHAIN.match(chain):
                chains[table].append(chain)
        elif line.startswith('-A ') and table:
            args = shlex.split(line)
            if not OUR_CHAIN.match(args[1]) and OUR_RULE.search(line):
                rules.append((table, args[1], args[2:]))
    for table, chain, args in rules:
        must(binary, '-w', '-t', table, '-D', chain, *args)
    for table, ours in chains.items():
        for chain in ours:
            must(binary, '-w', '-t', table, '-F', chain)
        for chain in ours:
            must(binary, '-w', '-t', table, '-X', chain)


def remove_nft_tables():
    if not shutil.which('nft'):
        return
    for line in run('nft', 'list', 'tables').stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == 'table' and OUR_NFT_TABLE.match(parts[2]):
            must('nft', 'delete', 'table', parts[1], parts[2])


def remove_ipsets():
    if not shutil.which('ipset'):
        return
    for name in run('ipset', 'list', '-n').stdout.split():
        if OUR_IPSET.match(name):
            must('ipset', 'destroy', name)


def main():
    global DATA_DIR
    DATA_DIR = os.path.realpath(sys.argv[1])
    for unit in ('k3s.service', 'k3s-agent.service'):
        run('systemctl', 'stop', unit)
    # ipset and friends may exist only in k3s's own bundle.
    bundles = os.path.join(DATA_DIR, 'data')
    for name in sorted(os.listdir(bundles)) if os.path.isdir(bundles) else []:
        path = os.path.join(bundles, name, 'bin')
        if os.path.isdir(path):
            os.environ['PATH'] += f':{path}:{path}/aux'
    kill_pods()
    for prefix in ('/run/k3s', '/var/lib/kubelet/pods', '/var/lib/kubelet/plugins', '/run/netns/cni-'):
        unmount_under(prefix)
    remove_netns()
    remove_links()
    remove_iptables('iptables')
    remove_iptables('ip6tables')
    remove_nft_tables()
    remove_ipsets()
    print(json.dumps({'changed': changed, 'errors': errors}))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
