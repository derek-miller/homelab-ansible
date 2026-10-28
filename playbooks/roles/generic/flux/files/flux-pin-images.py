#!/usr/bin/env python3
"""Pin every `image:` in a manifest tree to the digest its tag points at now.

Usage: flux-pin-images.py DIR

FLUX_PIN_MIRRORS maps a registry name the tree uses to the host that answers
for it, as JSON. A registry asking for basic auth gets the credentials
$DOCKER_CONFIG/config.json holds for its host. An image on a mirrored registry
that can't be resolved keeps its tag rather than failing the tree, since that
registry may be one this deploy brings up.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

IMAGE_LINE = re.compile(
    r"""^(?P<pre>\s*(?:-\s+)?image:\s*)(?P<q>["']?)(?P<ref>[^\s"'#]+)(?P=q)(?P<post>\s*(?:#.*)?)$"""
)

TRANSIENT = (429, 500, 502, 503, 504)


class ResolveError(Exception):
    pass


def parse(ref):
    head, sep, rest = ref.partition("/")
    if sep and ("." in head or ":" in head or head == "localhost"):
        registry, path = head, rest
    else:
        registry, path = "docker.io", ref
    name, colon, tag = path.rpartition(":")
    if not colon:
        name, tag = path, "latest"
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
        if "/" not in name:
            name = "library/" + name
    return registry, name, tag


def challenge(header):
    scheme, _, params = header.partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', params))


def _open(urlopen, request):
    for attempt in range(3):
        try:
            return urlopen(request, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code not in TRANSIENT or attempt == 2:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def token(header, name, urlopen):
    scheme, params = challenge(header)
    if scheme != "bearer" or "realm" not in params:
        raise ResolveError("the registry asked for %s auth" % (scheme or "unknown"))
    query = {
        "service": params.get("service"),
        "scope": params.get("scope") or "repository:%s:pull" % name,
    }
    url = params["realm"] + "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
    with _open(urlopen, urllib.request.Request(url)) as r:
        body = json.load(r)
    value = body.get("token") or body.get("access_token")
    if not value:
        raise ResolveError("the registry issued no token")
    return value


def load_auths(docker_config):
    try:
        with open(os.path.join(docker_config, "config.json"), encoding="utf-8") as f:
            auths = json.load(f).get("auths", {})
    except (OSError, ValueError):
        return {}
    return {host: v["auth"] for host, v in auths.items() if isinstance(v, dict) and v.get("auth")}


def resolve(ref, urlopen=urllib.request.urlopen, mirrors=None, auths=None):
    registry, name, tag = parse(ref)
    host = (mirrors or {}).get(registry, registry)
    url = "https://%s/v2/%s/manifests/%s" % (host, name, tag)
    headers = {"Accept": ACCEPT}
    try:
        while True:
            try:
                with _open(urlopen, urllib.request.Request(url, headers=headers, method="HEAD")) as r:
                    digest = r.headers.get("Docker-Content-Digest")
                if digest:
                    return digest
                with _open(urlopen, urllib.request.Request(url, headers=headers)) as r:
                    return "sha256:" + hashlib.sha256(r.read()).hexdigest()
            except urllib.error.HTTPError as e:
                if e.code != 401 or "Authorization" in headers:
                    raise
                challenge_header = e.headers.get("WWW-Authenticate", "")
                if challenge(challenge_header)[0] == "basic" and host in (auths or {}):
                    headers["Authorization"] = "Basic " + auths[host]
                else:
                    headers["Authorization"] = "Bearer " + token(challenge_header, name, urlopen)
    except urllib.error.HTTPError as e:
        raise ResolveError("HTTP %d" % e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ResolveError(type(e).__name__)


def pin_file(path, label, resolve, cache, errors, lenient=()):
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    pinned = 0
    for i, line in enumerate(lines):
        m = IMAGE_LINE.match(line)
        if not m or any(c in m["ref"] for c in "@${}"):
            continue
        ref = m["ref"]
        if ref not in cache:
            try:
                cache[ref] = resolve(ref)
            except ResolveError as e:
                cache[ref] = e
        found = cache[ref]
        # Name the file and line, never the image: the deploy log is public.
        if isinstance(found, ResolveError):
            if parse(ref)[0] not in lenient:
                errors.append("%s:%d: could not resolve the image: %s" % (label, i + 1, found))
            continue
        lines[i] = "%s%s%s@%s%s%s" % (m["pre"], m["q"], ref, found, m["q"], m["post"])
        pinned += 1
    if pinned:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return pinned


def pin_tree(root, resolve=resolve, lenient=()):
    cache, errors, pinned = {}, [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(dirpath, name)
            pinned += pin_file(path, os.path.relpath(path, root), resolve, cache, errors, lenient)
    left = sum(1 for ref, found in cache.items() if isinstance(found, ResolveError) and parse(ref)[0] in lenient)
    return pinned, len(cache), errors, left


def main(argv):
    if len(argv) != 2 or not os.path.isdir(argv[1]):
        print("usage: flux-pin-images.py DIR", file=sys.stderr)
        return 2
    mirrors = json.loads(os.environ.get("FLUX_PIN_MIRRORS") or "{}")
    auths = load_auths(os.environ.get("DOCKER_CONFIG") or os.path.expanduser("~/.docker"))
    pinned, images, errors, left = pin_tree(argv[1], lambda ref: resolve(ref, mirrors=mirrors, auths=auths), set(mirrors))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(json.dumps({"pinned": pinned, "images": images, "left": left}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
