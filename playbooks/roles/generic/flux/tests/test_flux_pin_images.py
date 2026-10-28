#!/usr/bin/env python3
"""Tests for flux-pin-images.py: which lines it pins, what it leaves alone,
how it talks to registries, and that its errors never name an image.

The registry is a stub urlopen answering from a canned table. Stdlib only,
since CI's venv holds just yamllint.
"""

import email.message
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCRIPT = os.path.join(ROLE, "files", "flux-pin-images.py")

loader = importlib.machinery.SourceFileLoader("flux_pin_images", SCRIPT)
spec = importlib.util.spec_from_loader("flux_pin_images", loader)
pin = importlib.util.module_from_spec(spec)
loader.exec_module(pin)
pin.time.sleep = lambda _: None

results = []


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("      got:  %r" % (got,))
        print("      want: %r" % (want,))
    results.append(ok)
    return ok


def headers(**values):
    h = email.message.Message()
    for k, v in values.items():
        h[k.replace("_", "-")] = v
    return h


class Response(io.BytesIO):
    def __init__(self, body=b"", **hdrs):
        super().__init__(body)
        self.headers = headers(**hdrs)


class Registry:
    """Answers HEAD/GET manifests and token requests from canned rules."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def __call__(self, request, timeout=None):
        method = request.get_method()
        auth = request.get_header("Authorization")
        self.calls.append((method, request.full_url, auth))
        answer = self.rules(method, request.full_url, auth)
        if isinstance(answer, Exception):
            raise answer
        return answer


def http_error(url, code, **hdrs):
    return urllib.error.HTTPError(url, code, "error", headers(**hdrs), None)


# parse: registries, implicit library/, tags, ports
check("parse: bare docker hub name", pin.parse("nginx"),
      ("registry-1.docker.io", "library/nginx", "latest"))
check("parse: docker hub org and tag", pin.parse("portainer/agent:latest"),
      ("registry-1.docker.io", "portainer/agent", "latest"))
check("parse: explicit docker.io", pin.parse("docker.io/library/redis:7"),
      ("registry-1.docker.io", "library/redis", "7"))
check("parse: ghcr", pin.parse("ghcr.io/project-zot/zot:v2.1.22"),
      ("ghcr.io", "project-zot/zot", "v2.1.22"))
check("parse: registry with a port and no tag", pin.parse("registry.local:5000/team/app"),
      ("registry.local:5000", "team/app", "latest"))
check("parse: registry with a port and a tag", pin.parse("localhost:5000/app:dev"),
      ("localhost:5000", "app", "dev"))

check("challenge: bearer params", pin.challenge(
    'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/nginx:pull"'),
    ("bearer", {"realm": "https://auth.docker.io/token", "service": "registry.docker.io",
                "scope": "repository:library/nginx:pull"}))

# resolve: anonymous HEAD, bearer challenge, GET fallback, failures
DIGEST = "sha256:" + "a" * 64


def anon(method, url, auth):
    return Response(Docker_Content_Digest=DIGEST)


reg = Registry(anon)
check("resolve: anonymous HEAD returns the digest header", pin.resolve("ghcr.io/o/app:1", reg), DIGEST)
check("resolve: asks the manifests endpoint by HEAD", reg.calls,
      [("HEAD", "https://ghcr.io/v2/o/app/manifests/1", None)])


def bearer(method, url, auth):
    if url.startswith("https://auth.example/token"):
        return Response(json.dumps({"token": "T"}).encode())
    if auth != "Bearer T":
        return http_error(url, 401, WWW_Authenticate='Bearer realm="https://auth.example/token",service="reg"')
    return Response(Docker_Content_Digest=DIGEST)


reg = Registry(bearer)
check("resolve: follows a bearer challenge", pin.resolve("reg.example/o/app:2", reg), DIGEST)
check("resolve: requests a pull token for the repository", reg.calls[1][1],
      "https://auth.example/token?service=reg&scope=repository%3Ao%2Fapp%3Apull")
check("resolve: retries the HEAD with the token", reg.calls[2], (
    "HEAD", "https://reg.example/v2/o/app/manifests/2", "Bearer T"))

BODY = b'{"schemaVersion":2}'


def no_header(method, url, auth):
    return Response(BODY)


check("resolve: hashes the manifest when HEAD has no digest header",
      pin.resolve("reg.example/o/app:3", Registry(no_header)),
      "sha256:" + __import__("hashlib").sha256(BODY).hexdigest())


def missing(method, url, auth):
    return http_error(url, 404)


def run_error(rules, ref):
    try:
        pin.resolve(ref, Registry(rules))
    except pin.ResolveError as e:
        return str(e)
    return None


check("resolve: a missing tag is an HTTP error", run_error(missing, "reg.example/o/app:4"), "HTTP 404")


def always_401(method, url, auth):
    if url.startswith("https://auth.example/token"):
        return Response(json.dumps({"token": "T"}).encode())
    return http_error(url, 401, WWW_Authenticate='Bearer realm="https://auth.example/token"')


check("resolve: a rejected token fails instead of looping",
      run_error(always_401, "reg.example/o/app:5"), "HTTP 401")


def basic_only(method, url, auth):
    return http_error(url, 401, WWW_Authenticate='Basic realm="zot"')


check("resolve: basic auth is refused, not guessed",
      run_error(basic_only, "reg.example/o/app:6"), "the registry asked for basic auth")


def basic_with_auth(method, url, auth):
    if auth == "Basic dXNlcjpwYXNz":
        return Response(Docker_Content_Digest=DIGEST)
    return http_error(url, 401, WWW_Authenticate='Basic realm="zot"')


reg = Registry(basic_with_auth)
check("resolve: a mirror's name is asked of its host, with that host's basic credentials",
      pin.resolve("registry.homelab.local/app:latest", reg,
                  mirrors={"registry.homelab.local": "registry.example"},
                  auths={"registry.example": "dXNlcjpwYXNz"}), DIGEST)
check("resolve: asks the mirror's host for the image",
      [c[1] for c in reg.calls], ["https://registry.example/v2/app/manifests/latest"] * 2)
try:
    pin.resolve("reg.example/o/app:6b", Registry(basic_with_auth), auths={"registry.example": "dXNlcjpwYXNz"})
    other_host = "resolved"
except pin.ResolveError as e:
    other_host = str(e)
check("resolve: basic credentials for another host are not sent", other_host, "the registry asked for basic auth")

with tempfile.TemporaryDirectory() as config:
    with open(os.path.join(config, "config.json"), "w") as f:
        json.dump({"auths": {"registry.example": {"auth": "dXNlcjpwYXNz"}, "10.43.0.5:5000": {}}}, f)
    check("load_auths: keeps each host's basic credentials", pin.load_auths(config), {"registry.example": "dXNlcjpwYXNz"})
    check("load_auths: a missing config is no credentials", pin.load_auths(os.path.join(config, "missing")), {})

flaky_count = {"n": 0}


def flaky(method, url, auth):
    flaky_count["n"] += 1
    if flaky_count["n"] < 3:
        return http_error(url, 503)
    return Response(Docker_Content_Digest=DIGEST)


check("resolve: retries transient registry errors", pin.resolve("reg.example/o/app:7", Registry(flaky)), DIGEST)


def offline(method, url, auth):
    return urllib.error.URLError("nodename nor servname provided")


check("resolve: a network failure names only its type",
      run_error(offline, "reg.example/secret-app:8"), "URLError")

# pin_tree: what gets rewritten, and what does not
MANIFEST = """---
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      initContainers:
        - image: busybox:1.36
      containers:
        - name: app
          image: "ghcr.io/o/app:latest"  # floats
        - name: sidecar
          image: 'ghcr.io/o/sidecar'
        - name: pinned
          image: ghcr.io/o/pinned:1@sha256:%s
        - name: templated
          image: ${IMAGE}
---
apiVersion: helm.cattle.io/v1
kind: HelmChart
spec:
  valuesContent: |-
    image:
      repository: ghcr.io/o/chart
      tag: "1.0"
    sidecar:
      image: ghcr.io/o/values:2
""" % ("b" * 64)

seen = []


def fake(ref):
    seen.append(ref)
    return "sha256:" + str(len(seen)) * 64


with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "web"))
    os.makedirs(os.path.join(root, ".hidden"))
    with open(os.path.join(root, "web", "app.yaml"), "w") as f:
        f.write(MANIFEST)
    with open(os.path.join(root, "web", "twice.yml"), "w") as f:
        f.write("---\nspec:\n  containers:\n    - image: busybox:1.36\n")
    for skipped in (os.path.join(root, ".hidden", "x.yaml"), os.path.join(root, "web", "._app.yaml"),
                    os.path.join(root, "web", "notes.txt")):
        with open(skipped, "w") as f:
            f.write("image: should-not-resolve:1\n")

    pinned, images, errors = pin.pin_tree(root, fake)
    with open(os.path.join(root, "web", "app.yaml")) as f:
        out = f.read()
    with open(os.path.join(root, "web", "twice.yml")) as f:
        twice = f.read()

    check("pin_tree: no errors", errors, [])
    check("pin_tree: resolves each distinct image once", sorted(seen), sorted([
        "busybox:1.36", "ghcr.io/o/app:latest", "ghcr.io/o/sidecar", "ghcr.io/o/values:2"]))
    check("pin_tree: counts pinned lines and distinct images", (pinned, images), (5, 4))
    d = {ref: "sha256:" + str(i + 1) * 64 for i, ref in enumerate(seen)}
    check("pin_tree: list-item image", "        - image: busybox:1.36@%s\n" % d["busybox:1.36"] in out, True)
    check("pin_tree: keeps double quotes and the trailing comment",
          '          image: "ghcr.io/o/app:latest@%s"  # floats\n' % d["ghcr.io/o/app:latest"] in out, True)
    check("pin_tree: keeps single quotes on an untagged image",
          "          image: 'ghcr.io/o/sidecar@%s'\n" % d["ghcr.io/o/sidecar"] in out, True)
    check("pin_tree: leaves an already pinned image", "image: ghcr.io/o/pinned:1@sha256:%s\n" % ("b" * 64) in out, True)
    check("pin_tree: leaves a substitution variable", "          image: ${IMAGE}\n" in out, True)
    check("pin_tree: leaves a repository/tag mapping", "    image:\n      repository: ghcr.io/o/chart\n" in out, True)
    check("pin_tree: pins an image inside a block scalar",
          "      image: ghcr.io/o/values:2@%s\n" % d["ghcr.io/o/values:2"] in out, True)
    check("pin_tree: reuses a digest across files", twice.endswith("busybox:1.36@%s\n" % d["busybox:1.36"]), True)
    check("pin_tree: ends the file as it found it", out.endswith('tag: "1.0"\n    sidecar:\n      image: ghcr.io/o/values:2@%s\n'
                                                                 % d["ghcr.io/o/values:2"]), True)

with tempfile.TemporaryDirectory() as root:
    with open(os.path.join(root, "media.vault.yaml"), "w") as f:
        f.write("---\nspec:\n  containers:\n    - image: lscr.io/o/secret-app:latest\n    - image: ok:1\n")

    def failing(ref):
        if "secret-app" in ref:
            raise pin.ResolveError("HTTP 404")
        return DIGEST

    pinned, images, errors = pin.pin_tree(root, failing)
    check("errors: name the file and line", errors, ["media.vault.yaml:4: could not resolve the image: HTTP 404"])
    check("errors: never name the image", any("secret-app" in e for e in errors), False)
    check("errors: still pin the images that resolved", pinned, 1)
    check("main: a missing directory is a usage error",
          pin.main(["flux-pin-images.py", os.path.join(root, "missing")]), 2)

failed = results.count(False)
print("\n%d passed, %d failed" % (results.count(True), failed))
sys.exit(1 if failed else 0)
