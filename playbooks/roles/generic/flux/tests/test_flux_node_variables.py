#!/usr/bin/env python3
"""Tests for flux-node-variables.py: which trees pass, what each rule refuses,
and that no error names an object."""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCRIPT = os.path.join(ROLE, "files", "flux-node-variables.py")

loader = importlib.machinery.SourceFileLoader("flux_node_variables", SCRIPT)
spec = importlib.util.spec_from_loader("flux_node_variables", loader)
nv = importlib.util.module_from_spec(spec)
loader.exec_module(nv)

LABELS = {"edge", "media"}
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
        return nv.check_tree(top, LABELS)


def claim(name="secretapp", storage="local-retain"):
    return """---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: %s
  namespace: edge
spec:
  storageClassName: %s
""" % (name, storage)


APP = """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: secretapp
  namespace: edge
spec:
  template:
    spec:
      nodeSelector:
        kubernetes.io/hostname: %s
"""

HELMRELEASE = """---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: traefik
  namespace: edge
spec:
  values:
    nodeSelector:
      kubernetes.io/hostname: ${node_edge}
    service:
      labels:
        svccontroller.k3s.cattle.io/lbpool: %s
    url: https://${node_edge}:7443
"""


def static_pv(node, namespace="edge"):
    return """---
kind: PersistentVolume
metadata:
  name: secretapp
spec:
  claimRef:
    namespace: %s
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - %s
""" % (namespace, node)


def names_nothing(errors):
    return not any("secretapp" in e for e in errors)


check("a conforming tree passes",
      run({"edge/app.yaml": claim() + APP % "${node_edge}" + HELMRELEASE % "${node_edge}"}), [])

errors = run({"edge/app.yaml": APP % "rackvm1"})
check("a literal node selector is refused", len(errors), 1)
check("that error names no object", names_nothing(errors), True)

check("another label's node selector is refused", len(run({"edge/app.yaml": APP % "${node_media}"})), 1)
check("a literal lbpool is refused", len(run({"edge/app.yaml": HELMRELEASE % "rackvm1"})), 1)
check("a quoted variable passes", run({"edge/app.yaml": HELMRELEASE % '"${node_edge}"'}), [])

errors = run({"edge/app.yaml": claim(storage="local-path")})
check("a claim on another class is refused", len(errors), 1)
check("that error names no object", names_nothing(errors), True)

check("a claim with no class is refused", len(run({"edge/app.yaml": claim().replace("  storageClassName: local-retain\n", "")})), 1)
check("a claim bound to a named volume passes",
      run({"edge/app.yaml": claim(storage="other").replace("spec:\n", "spec:\n  volumeName: v\n")}), [])

STATEFULSET = """---
kind: StatefulSet
metadata:
  name: secretapp
  namespace: edge
spec:
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
%s"""
errors = run({"edge/app.yaml": STATEFULSET % "        accessModes: [ReadWriteOnce]\n"})
check("a StatefulSet claim template with no class is refused", len(errors), 1)
check("that error names no object", names_nothing(errors), True)
check("a StatefulSet claim template on local-retain passes",
      run({"edge/app.yaml": STATEFULSET % "        storageClassName: local-retain\n"}), [])

check("a static volume on its label's variable passes", run({"pv.yaml": static_pv("${node_edge}")}), [])
check("a static volume on a literal node is refused", len(run({"pv.yaml": static_pv("rackvm1")})), 1)
check("a static volume outside a label is refused",
      len(run({"pv.yaml": static_pv("${node_nolabel}", namespace="nolabel")})) >= 1, True)

check("a stray $ is refused, by file and line",
      run({"edge/app.yaml": "---\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: edge\ndata:\n  run: echo $HOME\n"}),
      ["edge/app.yaml:7: a $ other than ${node_<label>} needs kustomize.toolkit.fluxcd.io/substitute: disabled"])

disabled = "---\nkind: ConfigMap\nmetadata:\n  name: c\n  annotations:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\ndata:\n  run: echo ${HOME} $$\n"
check("a disabled object may hold any other $", run({"edge/app.yaml": disabled}), [])
check("a disabled object can't use a node variable",
      len(run({"edge/app.yaml": disabled + "  node: ${node_edge}\n"})), 1)

check("an unknown label's variable is refused",
      len(run({"edge/app.yaml": "---\nkind: Secret\nstringData:\n  v: ${node_edge}${node_unknown}\n"})), 1)

check("every .yaml and .yml file is read, other files are not",
      len(run({"a.yaml": APP % "rackvm1", "b/c.yml": APP % "rackvm2", "skip.txt": "${x}"})), 2)

check("unparsable YAML is reported by line", run({"bad.yaml": "---\nkind: [\n"}), ["bad.yaml:2: not valid YAML"])

print("%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
