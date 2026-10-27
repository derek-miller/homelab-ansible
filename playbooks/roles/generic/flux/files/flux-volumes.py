#!/usr/bin/env python3
"""Check a manifest tree's node variables and list the folders its volumes need.

Usage: flux-volumes.py DIR LABELS ROOT

LABELS is the comma-separated [docker_labels] words and ROOT the folder every
local volume lives in. Prints {"volumes": [{"label", "path", "owner"}],
"errors": [...]}. An error names a file and line, never a volume, since a
hidden app's name must stay out of the deploy log.
"""

import json
import os
import re
import sys

import yaml

OWNER = "homelab.local/volume-owner"
NO_SUBSTITUTE = "kustomize.toolkit.fluxcd.io/substitute"
DOLLAR = re.compile(r"\$(\{[^}\n]*\}?)?")
LBPOOL = re.compile(r"svccontroller\.k3s\.cattle\.io/lbpool['\"]?\s*:\s*['\"]?([^'\"\s#]+)")
SEPARATOR = re.compile(r"^---\s*$")


def documents(path):
    """Yield (first line number, text) for each YAML document in a file."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    start, chunk = 1, []
    for number, line in enumerate(lines, 1):
        if SEPARATOR.match(line):
            if chunk:
                yield start, "\n".join(chunk)
            start, chunk = number + 1, []
        else:
            chunk.append(line)
    if chunk:
        yield start, "\n".join(chunk)


def substitution_disabled(doc):
    meta = doc.get("metadata") or {}
    return any((meta.get(k) or {}).get(NO_SUBSTITUTE) == "disabled"
               for k in ("annotations", "labels"))


def node_variable(label):
    return "${node_%s}" % label


def check_document(where, start, text, doc, labels, root):
    errors, volumes = [], []
    if not substitution_disabled(doc):
        for offset, line in enumerate(text.splitlines()):
            for match in DOLLAR.finditer(line):
                if match.group(0) not in {node_variable(label) for label in labels}:
                    errors.append("%s:%d: a $ other than ${node_<label>} needs %s: disabled"
                                  % (where, start + offset, NO_SUBSTITUTE))
    namespace = (doc.get("metadata") or {}).get("namespace")
    for offset, line in enumerate(text.splitlines()):
        match = LBPOOL.search(line)
        if match and not (namespace in labels and match.group(1) == node_variable(namespace)):
            errors.append("%s:%d: an lbpool must be ${node_<its namespace>}, a [docker_labels] word"
                          % (where, start + offset))
    if doc.get("kind") != "PersistentVolume":
        return errors, volumes
    spec = doc.get("spec") or {}
    meta = doc.get("metadata") or {}
    label = (spec.get("claimRef") or {}).get("namespace")
    terms = ((spec.get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms")
    if terms is not None:
        values = [v for term in terms for expr in term.get("matchExpressions") or []
                  for v in expr.get("values") or []]
        if label not in labels or values != [node_variable(label)]:
            errors.append("%s:%d: a PersistentVolume must claim into a [docker_labels] namespace and pin to ${node_<that namespace>}"
                          % (where, start))
    if "local" in spec:
        owner = str((meta.get("annotations") or {}).get(OWNER, ""))
        path = (spec.get("local") or {}).get("path")
        name = meta.get("name") or ""
        if not owner.isdigit() or path != os.path.join(root, name) or "/" in name or label not in labels:
            errors.append("%s:%d: a local PersistentVolume needs %s and the path %s/<its name>"
                          % (where, start, OWNER, root))
        else:
            volumes.append({"label": label, "path": path, "owner": int(owner)})
    return errors, volumes


def check_tree(top, labels, root):
    errors, volumes = [], []
    for directory, subdirs, files in os.walk(top):
        subdirs.sort()
        for name in sorted(files):
            if not name.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(directory, name)
            where = os.path.relpath(path, top)
            for start, text in documents(path):
                try:
                    doc = yaml.safe_load(text)
                except yaml.YAMLError:
                    errors.append("%s:%d: not valid YAML" % (where, start))
                    continue
                if not isinstance(doc, dict):
                    continue
                more_errors, more_volumes = check_document(where, start, text, doc, labels, root)
                errors += more_errors
                volumes += more_volumes
    return errors, volumes


def main(argv):
    if len(argv) != 4 or not os.path.isdir(argv[1]):
        print("usage: flux-volumes.py DIR LABELS ROOT", file=sys.stderr)
        return 2
    labels = {label for label in argv[2].split(",") if label}
    errors, volumes = check_tree(argv[1], labels, argv[3].rstrip("/"))
    print(json.dumps({"volumes": volumes, "errors": errors}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
