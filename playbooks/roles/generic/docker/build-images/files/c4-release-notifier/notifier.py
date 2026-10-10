"""Post new Control4 Composer and Control4.Jailbreak releases to Discord.

Two sources, each with its own webhook:
  composer   two feeds, one card per full version:
             installer      Control4's Updates SOAP service; a release is a
                            GetVersions entry ending in "+Composer", with its
                            installers listed by GetPackagesByVersion
             in-app update  Snap One's public resource catalog, which Composer's
                            own updater reads; it holds only the current build
                            per product, and betas are never announced
  jailbreak  GitHub releases of garrynewman/Control4.Jailbreak, keyed on tag

A release is announced the first time it is seen, not when it compares
greater than the last one: Composer moved from 4.x to 2026.M.D.build
numbering, so "newest" is not a stable ordering across the change.

To post a release again, e.g. to test the webhooks, drop it from state and
wait for the next poll or restart the service:
  python notifier.py forget composer 2026.9.16.642
  python notifier.py forget composer 2026.9.16.717
  python notifier.py forget jailbreak v9
"""

import contextlib
import fcntl
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import Callable, NamedTuple, Optional

import requests

# Values are stripped because ansible-vault encrypt_string keeps the trailing
# newline of whatever was typed.
COMPOSER_WEBHOOK_URL = os.environ.get("COMPOSER_WEBHOOK_URL", "").strip()
JAILBREAK_WEBHOOK_URL = os.environ.get("JAILBREAK_WEBHOOK_URL", "").strip()

STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1800"))
ONCE = os.environ.get("ONCE", "false").strip().lower() == "true"

UPDATES_NS = "http://services.control4.com/updates/v2_0/"
UPDATES_URL = "https://services.control4.com/Updates2x/v2_0/Updates.asmx"
# Composer's updater adds an X-Context header naming the dealer account, which
# makes the catalog serve that account's targeted betas.
CATALOG_URL = "https://resources.snapone.com/api/v1/public/resources/application"
JAILBREAK_REPO = os.environ.get("JAILBREAK_REPO", "garrynewman/Control4.Jailbreak").strip()
RESCAN_NEWEST_VERSIONS = 3

USER_AGENT = "c4-release-notifier"
COMPOSER_COLOR = 0xE6332A
JAILBREAK_COLOR = 0x24292F

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
jailbreak_cache = {"etag": None, "releases": []}


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


@contextlib.contextmanager
def locked_state():
    """Read, modify and write state under a lock shared with `forget`.

    Every write goes through here and starts from the file rather than a copy
    held across a poll, so a `forget` that lands mid-poll is not overwritten.
    """
    with open(f"{STATE_FILE}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state()
        yield state
        save_state(state)


def soap(action, inner):
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:upd="{UPDATES_NS}"><soap:Body>{inner}</soap:Body></soap:Envelope>'
    )
    r = session.post(
        UPDATES_URL,
        data=body.encode(),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{UPDATES_NS}{action}"',
        },
        timeout=30,
    )
    r.raise_for_status()
    return ET.fromstring(r.content)


def version_key(version):
    return tuple(int(p) for p in re.findall(r"\d+", version.split("-")[0]))


def composer_versions():
    root = soap(
        "GetVersions",
        "<upd:GetVersions><upd:currentVersion>3.0.0</upd:currentVersion></upd:GetVersions>",
    )
    found = [e.text for e in root.iter(f"{{{UPDATES_NS}}}string") if e.text]
    return sorted((v for v in found if v.endswith("+Composer")), key=version_key)


def composer_packages(version):
    root = soap(
        "GetPackagesByVersion",
        "<upd:GetPackagesByVersion><upd:version>"
        f"{version}"
        "</upd:version></upd:GetPackagesByVersion>",
    )
    packages = []
    for pkg in root.iter(f"{{{UPDATES_NS}}}Package"):
        name = pkg.findtext(f"{{{UPDATES_NS}}}Name", "")
        if not name.lower().startswith("composer"):
            continue
        packages.append(
            {
                "name": name,
                "url": pkg.findtext(f"{{{UPDATES_NS}}}Url", "").replace("http://", "https://", 1),
                "size": int(pkg.findtext(f"{{{UPDATES_NS}}}Size", "0") or 0),
            }
        )
    return sorted(packages, key=lambda p: p["name"])


def composer_installers():
    """Return one item per Composer installer, oldest version first.

    Every version is scanned on first start. After that only versions not yet
    marked scanned and the newest RESCAN_NEWEST_VERSIONS are, so an installer
    added to a recent version later is still found. A version is marked
    scanned once every installer it lists is in the posted set, so one whose
    post failed is fetched again next poll.

    A version whose lookup fails is skipped and fetched again next poll. If
    that happens on first start, the version is recorded in
    composer_unseeded, and its installers are added as already posted once
    a lookup succeeds, since it existed before the notifier did.
    """
    state = load_state()
    ordered = composer_versions()
    seeding = "composer" not in state
    if seeding:
        to_scan = ordered
    else:
        scanned = set(state.get("composer_scanned", []))
        recent = set(ordered[-RESCAN_NEWEST_VERSIONS:])
        to_scan = [v for v in ordered if v not in scanned or v in recent]
    unseeded = set(state.get("composer_unseeded", []))
    items, failed, fetched, empty, quiet = [], set(), set(), set(), []
    for version in to_scan:
        try:
            found = [
                {**p, "version": version, "feed": "installer"} for p in composer_packages(version)
            ]
        except Exception as e:
            log(f"composer: looking up {version} failed, will retry: {describe(e)}")
            failed.add(version)
            continue
        fetched.add(version)
        if not found:
            empty.add(version)
        elif version in unseeded and not seeding:
            quiet.extend(found)
        else:
            items.extend(found)
    if seeding and ordered and ordered[-1] in failed:
        raise RuntimeError(f"looking up the newest version {ordered[-1]} failed")
    with locked_state() as s:
        if seeding:
            unseeded |= failed
        s["composer_unseeded"] = sorted(unseeded - fetched)
        if quiet:
            s["composer"] = sorted(set(s["composer"]) | {composer_key(i) for i in quiet})
            empty |= {i["version"] for i in quiet}
        s["composer_scanned"] = sorted(set(s.get("composer_scanned", [])) | empty)
    return items


def catalog_installers():
    """Return the catalog's current general-release Composer installers.

    Betas are dropped here, never recorded, so one announces if it is later
    promoted. Recorded without posting: everything on the first successful
    fetch over existing state, and any build whose full version the installer
    feed has already announced.
    """
    entries, page, pages = [], 1, 1
    while page <= pages:
        r = session.get(CATALOG_URL, params={"page": page}, timeout=30)
        r.raise_for_status()
        body = r.json()
        entries.extend(body["data"])
        pages = body["pagination"]["totalPages"]
        page += 1
    items = [
        {
            "name": e["filename"],
            "version": e["metadata"]["fullVersion"],
            "size": e["fileSize"],
            "feed": "in-app update",
        }
        for e in entries
        if e.get("isBeta") is False and e["filename"].lower().startswith("composer")
    ]
    with locked_state() as s:
        if "composer" in s:
            seeding = not s.get("composer_catalog_seeded")
            announced = {
                full_version(k.split("/", 1)[0]) for k in s["composer"] if "+Composer" in k
            }
            quiet = [i for i in items if seeding or i["version"] in announced]
            s["composer"] = sorted(set(s["composer"]) | {composer_key(i) for i in quiet})
            if seeding:
                log(f"composer: recorded {len(quiet)} in-app update items without posting")
        s["composer_catalog_seeded"] = True
    return items


def composer_items():
    """Return both feeds' items, oldest full version first.

    Either feed failing is logged and the other still runs, except before the
    first seed, when the installer feed must succeed so check() seeds from it.
    """
    try:
        items = composer_installers()
    except Exception as e:
        if "composer" not in load_state():
            raise
        log(f"composer: installer feed failed: {describe(e)}")
        items = []
    try:
        items += catalog_installers()
    except Exception as e:
        log(f"composer: in-app update feed failed: {describe(e)}")
    return sorted(items, key=lambda i: version_key(i["version"]))


def full_version(version):
    return version.removesuffix("+Composer").removesuffix("-res")


def composer_settle(state, items):
    posted = set(state.get("composer", []))
    keys = {}
    for i in items:
        if i["feed"] != "installer":
            continue
        keys.setdefault(i["version"], []).append(composer_key(i))
    done = {v for v, ks in keys.items() if all(k in posted for k in ks)}
    state["composer_scanned"] = sorted(set(state.get("composer_scanned", [])) | done)


def composer_key(item):
    return f"{item['version']}/{item['name']}"


def installer_label(name):
    return re.sub(r"^Composer(?=\w)", "Composer ", re.split(r"[-_]", name)[0])


def composer_embed(installers):
    feeds, lines = [], []
    for feed in ("installer", "in-app update"):
        members = [i for i in installers if i["feed"] == feed]
        if not members:
            continue
        feeds.append(feed)
        if feed == "in-app update":
            lines.append("Available through Composer's in-app update:")
        for i in members:
            size = f"{i['size'] / 1e6:.0f} MB"
            if feed == "installer":
                size = f"[download]({i['url']}) ({size})"
            lines.append(f"**{installer_label(i['name'])}**: {size}")
    return {
        "author": {"name": "Control4 Composer"},
        "title": f"{full_version(installers[0]['version'])} ({', '.join(feeds)})",
        "color": COMPOSER_COLOR,
        "description": "\n".join(lines),
    }


def jailbreak_releases():
    """Return every published release, oldest first.

    All pages are fetched so `forget` works for any tag. Only the first page
    is conditional: a new release always lands on it.
    """
    accept = {"Accept": "application/vnd.github+json"}
    headers = dict(accept)
    if jailbreak_cache["etag"]:
        headers["If-None-Match"] = jailbreak_cache["etag"]
    r = session.get(
        f"https://api.github.com/repos/{JAILBREAK_REPO}/releases",
        params={"per_page": 100},
        headers=headers,
        timeout=30,
    )
    if r.status_code == 304:
        return jailbreak_cache["releases"]
    r.raise_for_status()
    etag = r.headers.get("ETag")
    found = r.json()
    while "next" in r.links:
        r = session.get(r.links["next"]["url"], headers=accept, timeout=30)
        r.raise_for_status()
        found.extend(r.json())
    releases = [rel for rel in found if not rel.get("draft") and not rel.get("prerelease")]
    releases.sort(key=lambda rel: rel.get("published_at") or "")
    jailbreak_cache.update(etag=etag, releases=releases)
    return releases


def release_tag(release):
    return release["tag_name"]


def jailbreak_embed(releases):
    release = releases[0]
    title = release.get("name") or release["tag_name"]
    assets = [
        f"**{a['name']}**: [download]({a['browser_download_url']})"
        for a in release.get("assets", [])
    ]
    author = {"name": JAILBREAK_REPO, "url": f"https://github.com/{JAILBREAK_REPO}"}
    if release.get("author", {}).get("avatar_url"):
        author["icon_url"] = release["author"]["avatar_url"]
    return {
        "author": author,
        "title": title,
        "url": release["html_url"],
        "color": JAILBREAK_COLOR,
        "description": "\n".join(assets) or "No release assets.",
    }


def describe(e):
    # requests errors embed the request URL, and a webhook URL holds its token.
    if isinstance(e, requests.RequestException):
        status = getattr(e.response, "status_code", None)
        return f"{type(e).__name__} {status}" if status else type(e).__name__
    return f"{type(e).__name__}: {e}"


def post(webhook, embed):
    for _ in range(5):
        r = session.post(webhook, json={"embeds": [embed]}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate limiting the webhook")


class Source(NamedTuple):
    name: str
    webhook: str
    fetch: Callable[[], list]
    key: Callable[[dict], str]
    group: Callable[[dict], str]
    embed: Callable[[list], dict]
    settle: Optional[Callable[[dict, list], None]] = None


def check(source):
    """Fetch items oldest first and post the new ones, one message per group.

    With no state yet, post only the newest group and record the rest as seen.
    """
    name, key, group = source.name, source.key, source.group
    try:
        items = source.fetch()
    except Exception as e:
        log(f"{name}: fetch failed: {describe(e)}")
        return
    with locked_state() as state:
        if name in state:
            seen = set(state[name])
            new = [i for i in items if key(i) not in seen]
        else:
            newest = group(items[-1]) if items else None
            new = [i for i in items if group(i) == newest]
            state[name] = sorted(key(i) for i in items if group(i) != newest)
            log(f"{name}: seeded {len(state[name])} older items, posting the newest")
        if source.settle:
            source.settle(state, items)
    groups = {}
    for item in new:
        groups.setdefault(group(item), []).append(item)
    for label, members in groups.items():
        try:
            post(source.webhook, source.embed(members))
        except Exception as e:
            log(f"{name}: posting {label} failed, will retry next poll: {describe(e)}")
            continue
        with locked_state() as state:
            state[name] = sorted(set(state.get(name, [])) | {key(i) for i in members})
            if source.settle:
                source.settle(state, items)
        log(f"{name}: posted {label} ({len(members)} items)")
    log(f"{name}: {len(items)} items, {len(groups)} to post")


def forget(source, target):
    """Drop matching entries from state so the next poll posts them again."""
    with locked_state() as state:
        dropped = forget_entries(state, source, target)
    for k in dropped:
        print(f"forgot {source} {k}")


def forget_entries(state, source, target):
    if source not in state:
        sys.exit(f"no {source} state in {STATE_FILE}")

    def matches(k):
        if k == target:
            return True
        if source != "composer" or "/" in target:
            return False
        return version_key(k.split("/", 1)[0]) == version_key(target)

    dropped = [k for k in state[source] if matches(k)]
    if not dropped:
        sys.exit(f"nothing in {source} state matches {target}")
    state[source] = [k for k in state[source] if k not in dropped]
    if source == "composer":
        versions = {k.split("/", 1)[0] for k in dropped}
        state["composer_scanned"] = [
            v for v in state.get("composer_scanned", []) if v not in versions
        ]
    return dropped


def main():
    if sys.argv[1:2] == ["forget"]:
        if len(sys.argv) != 4 or sys.argv[2] not in ("composer", "jailbreak"):
            sys.exit("usage: notifier.py forget composer|jailbreak <version, tag or state entry>")
        forget(sys.argv[2], sys.argv[3])
        return
    if not COMPOSER_WEBHOOK_URL and not JAILBREAK_WEBHOOK_URL:
        sys.exit("set COMPOSER_WEBHOOK_URL and/or JAILBREAK_WEBHOOK_URL")
    sources = [
        Source(
            name="composer",
            webhook=COMPOSER_WEBHOOK_URL,
            fetch=composer_items,
            key=composer_key,
            group=lambda i: full_version(i["version"]),
            embed=composer_embed,
            settle=composer_settle,
        ),
        Source(
            name="jailbreak",
            webhook=JAILBREAK_WEBHOOK_URL,
            fetch=jailbreak_releases,
            key=release_tag,
            group=release_tag,
            embed=jailbreak_embed,
        ),
    ]
    sources = [s for s in sources if s.webhook]
    while True:
        for source in sources:
            try:
                check(source)
            except Exception as e:
                log(f"{source.name}: {describe(e)}")
        if ONCE:
            return
        time.sleep(POLL_INTERVAL + random.uniform(0, POLL_INTERVAL / 10))


if __name__ == "__main__":
    main()
