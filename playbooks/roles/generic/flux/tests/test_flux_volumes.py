#!/usr/bin/env python3
"""Tests for flux-volumes.py: which trees pass, what each rule refuses, and
that no error names a volume or its path."""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCRIPT = os.path.join(ROLE, "files", "flux-volumes.py")

loader = importlib.machinery.SourceFileLoader("flux_volumes", SCRIPT)
spec = importlib.util.spec_from_loader("flux_volumes", loader)
vol = importlib.util.module_from_spec(spec)
loader.exec_module(vol)

LABELS = {"edge", "media"}
ROOT = "/mnt/data/volumes"
results = []


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("      got:  %r" % (got,))
        print("      want: %r" % (want,))
    results.append(ok)
    return ok


def run(files):
    with tempfile.TemporaryDirectory() as top:
        for rel, text in files.items():
            path = os.path.join(top, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return vol.check_tree(top, LABELS, ROOT)


def pv(name="secretapp", namespace="edge", path=None, owner='"65532"', node="${node_edge}"):
    owner_line = "\n  annotations:\n    homelab.local/volume-owner: %s" % owner if owner else ""
    return """---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: %s%s
spec:
  claimRef:
    namespace: %s
    name: data
  local:
    path: %s
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - %s
""" % (name, owner_line, namespace, path or "%s/%s" % (ROOT, name), node)


HELMRELEASE = """---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: traefik
  namespace: edge
spec:
  values:
    service:
      labels:
        svccontroller.k3s.cattle.io/lbpool: %s
    url: https://${node_edge}:7443
"""


def no_secret(errors):
    return not any("secretapp" in e or "/mnt/data/elsewhere" in e for e in errors)


errors, volumes = run({"edge/app.yaml": pv() + HELMRELEASE % "${node_edge}"})
check("a conforming tree passes", errors, [])
check("its volume is listed", volumes, [{"label": "edge", "path": ROOT + "/secretapp", "owner": 65532}])

errors, _ = run({"edge/app.yaml": pv(node="rackvm1")})
check("a literal node is refused", len(errors), 1)
check("that error names no volume", no_secret(errors), True)

errors, _ = run({"edge/app.yaml": pv(node="${node_media}")})
check("another label's node is refused", len(errors), 1)

errors, _ = run({"edge/app.yaml": pv(namespace="nolabel", node="${node_nolabel}")})
check("a namespace that is no label is refused", len(errors) >= 1, True)

errors, volumes = run({"edge/app.yaml": pv(path="/mnt/data/elsewhere")})
check("a path outside ROOT/<name> is refused", (len(errors), volumes), (1, []))
check("that error names no path", no_secret(errors), True)

errors, _ = run({"edge/app.yaml": pv(owner=None)})
check("a local volume needs an owner", len(errors), 1)

errors, _ = run({"edge/app.yaml": pv(owner='"nobody"')})
check("the owner must be numeric", len(errors), 1)

errors, _ = run({"edge/app.yaml": HELMRELEASE % "rackvm1"})
check("a literal lbpool is refused", len(errors), 1)

errors, _ = run({"edge/app.yaml": HELMRELEASE % '"${node_media}"'})
check("another label's lbpool is refused", len(errors), 1)

errors, _ = run({"edge/app.yaml": "---\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: edge\ndata:\n  run: echo $HOME\n"})
check("a stray $ is refused, by file and line", errors, ["edge/app.yaml:7: a $ other than ${node_<label>} needs kustomize.toolkit.fluxcd.io/substitute: disabled"])

errors, _ = run({"edge/app.yaml": "---\nkind: ConfigMap\nmetadata:\n  name: c\n  annotations:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\ndata:\n  run: echo ${HOME} $$\n"})
check("a disabled object may hold any $", errors, [])

errors, _ = run({"edge/app.yaml": "---\nkind: Secret\nstringData:\n  v: ${node_edge}${node_unknown}\n"})
check("an unknown label's variable is refused", len(errors), 1)

errors, volumes = run({"a.yaml": pv(name="one"), "b/c.yml": pv(name="two", namespace="media", node="${node_media}"), "skip.txt": "${x}"})
check("every .yaml and .yml file is read, other files are not", (errors, len(volumes)), ([], 2))

errors, _ = run({"bad.yaml": "---\nkind: [\n"})
check("unparsable YAML is reported by line", errors, ["bad.yaml:2: not valid YAML"])

hostpath = pv().replace("  local:\n    path: %s/secretapp\n" % ROOT, "  hostPath:\n    path: /mnt/share\n")
errors, volumes = run({"edge/app.yaml": hostpath})
check("a non-local volume is checked but gets no folder", (errors, volumes), ([], []))

print("%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
