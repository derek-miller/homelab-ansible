#!/usr/bin/env python3
"""Check a manifest tree pins to nodes only through ${node_<label>}.

Usage: flux-node-variables.py DIR LABELS

LABELS is the comma-separated [docker_labels] words. Prints {"errors": [...]}.
An error names a file and line, never an object, since a hidden app's name
must stay out of the deploy log.
"""

import json
import os
import re
import sys

import yaml

STORAGE_CLASS = "local-retain"
NO_SUBSTITUTE = "kustomize.toolkit.fluxcd.io/substitute"
DOLLAR = re.compile(r"\$(\{[^}\n]*\}?)?")
PIN = re.compile(r"(svccontroller\.k3s\.cattle\.io/lbpool|kubernetes\.io/hostname)['\"]?\s*:\s*['\"]?([^'\"\s#]+)")
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


def check_document(where, start, text, doc, labels):
    errors = []
    lines = text.splitlines()
    allowed = {node_variable(label) for label in labels}
    disabled = substitution_disabled(doc)
    for offset, line in enumerate(lines):
        for match in DOLLAR.finditer(line):
            if disabled and match.group(0) in allowed:
                errors.append("%s:%d: an object with %s: disabled can't use ${node_<label>}"
                              % (where, start + offset, NO_SUBSTITUTE))
            elif not disabled and match.group(0) not in allowed:
                errors.append("%s:%d: a $ other than ${node_<label>} needs %s: disabled"
                              % (where, start + offset, NO_SUBSTITUTE))
    namespace = (doc.get("metadata") or {}).get("namespace")
    for offset, line in enumerate(lines):
        match = PIN.search(line)
        if match and not (namespace in labels and match.group(2) == node_variable(namespace)):
            errors.append("%s:%d: a %s must be ${node_<its namespace>}, a [docker_labels] word"
                          % (where, start + offset, match.group(1)))
    spec = doc.get("spec") or {}
    if doc.get("kind") == "PersistentVolume":
        label = (spec.get("claimRef") or {}).get("namespace")
        terms = ((spec.get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms")
        values = [v for term in terms or [] for expr in term.get("matchExpressions") or []
                  for v in expr.get("values") or []]
        if terms is not None and (label not in labels or values != [node_variable(label)]):
            errors.append("%s:%d: a PersistentVolume must claim into a [docker_labels] namespace and pin to ${node_<that namespace>}"
                          % (where, start))
    if doc.get("kind") == "PersistentVolumeClaim":
        claims = [spec]
    elif doc.get("kind") == "StatefulSet":
        claims = [(t or {}).get("spec") or {} for t in spec.get("volumeClaimTemplates") or []]
    else:
        claims = []
    if any(not c.get("volumeName") and c.get("storageClassName") != STORAGE_CLASS for c in claims):
        errors.append("%s:%d: a PersistentVolumeClaim needs storageClassName: %s"
                      % (where, start, STORAGE_CLASS))
    return errors


def check_tree(top, labels):
    errors = []
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
                if isinstance(doc, dict):
                    errors += check_document(where, start, text, doc, labels)
    return errors


def main(argv):
    if len(argv) != 3 or not os.path.isdir(argv[1]):
        print("usage: flux-node-variables.py DIR LABELS", file=sys.stderr)
        return 2
    labels = {label for label in argv[2].split(",") if label}
    print(json.dumps({"errors": check_tree(argv[1], labels)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
