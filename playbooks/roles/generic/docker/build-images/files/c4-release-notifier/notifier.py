"""Post new Control4 Composer and Control4.Jailbreak releases to Discord.

Two sources, each with its own webhook:
  composer   Control4's Updates SOAP service; a release is a GetVersions
             entry ending in "+Composer", with its installers listed by
             GetPackagesByVersion
  jailbreak  GitHub releases of garrynewman/Control4.Jailbreak, keyed on tag

A release is announced the first time it is seen, not when it compares
greater than the last one: Composer moved from 4.x to 2026.M.D.build
numbering, so "newest" is not a stable ordering across the change.

To post a release again, e.g. to test the webhooks, drop it from state and
wait for the next poll or restart the service:
  python notifier.py forget composer 2026.9.16.642
  python notifier.py forget jailbreak v9
"""

import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET

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
JAILBREAK_REPO = os.environ.get("JAILBREAK_REPO", "garrynewman/Control4.Jailbreak").strip()
RESCAN_NEWEST_VERSIONS = 3

USER_AGENT = "c4-release-notifier (+https://github.com/derek-miller/homelab-ansible)"
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


def composer_installers(state):
    """Return one item per Composer installer, oldest version first.

    Every version is scanned on first start. After that only versions not yet
    marked scanned and the newest RESCAN_NEWEST_VERSIONS are, so an installer
    added to a recent version later is still found. A version is marked
    scanned once every installer it lists is in the posted set, so one whose
    post failed is fetched again next poll.
    """
    ordered = composer_versions()
    scanned = set(state.get("composer_scanned", []))
    posted = set(state.get("composer", []))
    if "composer" in state:
        recent = set(ordered[-RESCAN_NEWEST_VERSIONS:])
        to_scan = [v for v in ordered if v not in scanned or v in recent]
    else:
        to_scan = ordered
    items = []
    for version in to_scan:
        found = [{**p, "version": version} for p in composer_packages(version)]
        items.extend(found)
        if all(composer_key(i) in posted for i in found):
            scanned.add(version)
    state["composer_scanned"] = sorted(scanned)
    return items


def composer_key(item):
    return f"{item['version']}/{item['name']}"


def installer_label(name):
    return re.sub(r"^Composer(?=\w)", "Composer ", name.split("-")[0])


def composer_embed(installers):
    version = installers[0]["version"].removesuffix("+Composer").removesuffix("-res")
    lines = [
        f"**{installer_label(i['name'])}**: [download]({i['url']}) ({i['size'] / 1e6:.0f} MB)"
        for i in installers
    ]
    return {
        "author": {"name": "Control4 Composer"},
        "title": version,
        "color": COMPOSER_COLOR,
        "description": "\n".join(lines),
    }


def jailbreak_releases(state):
    headers = {"Accept": "application/vnd.github+json"}
    if jailbreak_cache["etag"]:
        headers["If-None-Match"] = jailbreak_cache["etag"]
    r = session.get(
        f"https://api.github.com/repos/{JAILBREAK_REPO}/releases",
        params={"per_page": 10},
        headers=headers,
        timeout=30,
    )
    if r.status_code == 304:
        return jailbreak_cache["releases"]
    r.raise_for_status()
    releases = [rel for rel in r.json() if not rel.get("draft") and not rel.get("prerelease")]
    releases.sort(key=lambda rel: rel.get("published_at") or "")
    jailbreak_cache.update(etag=r.headers.get("ETag"), releases=releases)
    return releases


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


def check(state, name, webhook, fetch, key, group, embed):
    """Fetch items oldest first and post the new ones, one message per group.

    With no state yet, post only the newest group and record the rest as seen.
    """
    try:
        items = fetch(state)
    except Exception as e:
        log(f"{name}: fetch failed: {describe(e)}")
        return
    if name in state:
        seen = set(state[name])
        new = [i for i in items if key(i) not in seen]
    else:
        newest = group(items[-1]) if items else None
        new = [i for i in items if group(i) == newest]
        seen = {key(i) for i in items if group(i) != newest}
        state[name] = sorted(seen)
        log(f"{name}: seeded {len(seen)} older items, posting the newest")
    save_state(state)
    groups = {}
    for item in new:
        groups.setdefault(group(item), []).append(item)
    for label, members in groups.items():
        try:
            post(webhook, embed(members))
        except Exception as e:
            log(f"{name}: posting {label} failed, will retry next poll: {describe(e)}")
            continue
        seen.update(key(i) for i in members)
        state[name] = sorted(seen)
        save_state(state)
        log(f"{name}: posted {label} ({len(members)} items)")
    log(f"{name}: {len(items)} items, {len(groups)} to post")


def forget(source, target):
    """Drop matching entries from state so the next poll posts them again."""
    state = load_state()
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
    save_state(state)
    for k in dropped:
        print(f"forgot {source} {k}")


def main():
    if sys.argv[1:2] == ["forget"]:
        if len(sys.argv) != 4 or sys.argv[2] not in ("composer", "jailbreak"):
            sys.exit("usage: notifier.py forget composer|jailbreak <version, tag or state entry>")
        forget(sys.argv[2], sys.argv[3])
        return
    if not COMPOSER_WEBHOOK_URL and not JAILBREAK_WEBHOOK_URL:
        sys.exit("set COMPOSER_WEBHOOK_URL and/or JAILBREAK_WEBHOOK_URL")
    sources = [
        (
            "composer",
            COMPOSER_WEBHOOK_URL,
            composer_installers,
            composer_key,
            lambda i: i["version"],
            composer_embed,
        ),
        (
            "jailbreak",
            JAILBREAK_WEBHOOK_URL,
            jailbreak_releases,
            lambda r: r["tag_name"],
            lambda r: r["tag_name"],
            jailbreak_embed,
        ),
    ]
    sources = [s for s in sources if s[1]]
    while True:
        # Reloaded every poll so a `forget` run alongside the service sticks.
        state = load_state()
        for source in sources:
            try:
                check(state, *source)
            except Exception as e:
                log(f"{source[0]}: {describe(e)}")
        if ONCE:
            return
        time.sleep(POLL_INTERVAL + random.uniform(0, POLL_INTERVAL / 10))


if __name__ == "__main__":
    main()
