"""Post new Control4 Composer and Control4.Jailbreak releases to Discord.

Two sources, each with its own webhook:
  composer   Control4's Updates SOAP service; a release is a GetVersions
             entry ending in "+Composer", with its installers listed by
             GetPackagesByVersion
  jailbreak  GitHub releases of garrynewman/Control4.Jailbreak, keyed on tag

A release is announced the first time it is seen, not when it compares
greater than the last one: Composer moved from 4.x to 2026.M.D.build
numbering, so "newest" is not a stable ordering across the change.
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
NOTIFY_ON_SEED = os.environ.get("NOTIFY_ON_SEED", "false").strip().lower() == "true"
# The external endpoint carries beta OS/Composer builds that never reach the
# main one until GA.
INCLUDE_BETA = os.environ.get("INCLUDE_BETA", "false").strip().lower() == "true"
ONCE = os.environ.get("ONCE", "false").strip().lower() == "true"

UPDATES_NS = "http://services.control4.com/updates/v2_0/"
UPDATES_URL = "https://services.control4.com/Updates2x/v2_0/Updates.asmx"
UPDATES_BETA_URL = "https://services.control4.com/Updates2x-external/v2_0/Updates.asmx"
JAILBREAK_REPO = os.environ.get("JAILBREAK_REPO", "garrynewman/Control4.Jailbreak").strip()

USER_AGENT = "c4-release-notifier (+https://github.com/derek-miller/homelab-ansible)"
COMPOSER_COLOR = 0xE6332A
JAILBREAK_COLOR = 0x24292F

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


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


def soap(url, action, inner):
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:upd="{UPDATES_NS}"><soap:Body>{inner}</soap:Body></soap:Envelope>'
    )
    r = session.post(
        url,
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


def composer_versions(url):
    root = soap(
        url,
        "GetVersions",
        "<upd:GetVersions><upd:currentVersion>3.0.0</upd:currentVersion></upd:GetVersions>",
    )
    found = [e.text for e in root.iter(f"{{{UPDATES_NS}}}string") if e.text]
    return sorted((v for v in found if v.endswith("+Composer")), key=version_key)


def composer_packages(url, version):
    root = soap(
        url,
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


def composer_releases():
    endpoints = [(UPDATES_URL, False)]
    if INCLUDE_BETA:
        endpoints.append((UPDATES_BETA_URL, True))
    releases = {}
    for url, beta in endpoints:
        for version in composer_versions(url):
            if version in releases:
                continue
            releases[version] = {"version": version, "url": url, "beta": beta}
    return list(releases.values())


def composer_embed(release):
    version = release["version"].removesuffix("+Composer")
    packages = composer_packages(release["url"], release["version"])
    lines = [f"[{p['name']}]({p['url']}) ({p['size'] / 1e6:.0f} MB)" for p in packages]
    title = f"Composer {version}"
    if release["beta"]:
        title += " (beta)"
    embed = {
        "title": title,
        "color": COMPOSER_COLOR,
        "description": "\n".join(lines) or "No installer packages listed.",
        "footer": {"text": "Control4 Updates service"},
    }
    return embed


def jailbreak_releases():
    r = session.get(
        f"https://api.github.com/repos/{JAILBREAK_REPO}/releases",
        params={"per_page": 10},
        headers={"Accept": "application/vnd.github+json"},
        timeout=30,
    )
    r.raise_for_status()
    releases = [rel for rel in r.json() if not rel.get("draft")]
    return sorted(releases, key=lambda rel: rel.get("published_at") or "")


def jailbreak_embed(release):
    title = release.get("name") or release["tag_name"]
    if release.get("prerelease"):
        title += " (prerelease)"
    assets = [f"[{a['name']}]({a['browser_download_url']})" for a in release.get("assets", [])]
    embed = {
        "title": title,
        "url": release["html_url"],
        "color": JAILBREAK_COLOR,
        "description": "\n".join(assets) or "No release assets.",
        "footer": {"text": f"{JAILBREAK_REPO} {release['tag_name']}"},
    }
    if release.get("published_at"):
        embed["timestamp"] = release["published_at"]
    return embed


def post(webhook, embed):
    for _ in range(5):
        r = session.post(webhook, json={"embeds": [embed]}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate limiting the webhook")


def check(state, name, webhook, fetch, key, embed):
    """Fetch releases oldest first and post the ones not yet in state."""
    try:
        items = fetch()
    except (requests.RequestException, ET.ParseError) as e:
        log(f"{name}: fetch failed: {e}")
        return
    if name in state:
        seen = set(state[name])
        to_post = [i for i in items if key(i) not in seen]
    else:
        to_post = items[-1:] if NOTIFY_ON_SEED else []
        seen = {key(i) for i in items} - {key(i) for i in to_post}
        state[name] = sorted(seen)
        save_state(state)
        log(f"{name}: seeded {len(seen)} existing releases without posting")
    for item in to_post:
        try:
            post(webhook, embed(item))
        except (requests.RequestException, ET.ParseError, RuntimeError) as e:
            log(f"{name}: posting {key(item)} failed, will retry next poll: {e}")
            continue
        seen.add(key(item))
        state[name] = sorted(seen)
        save_state(state)
        log(f"{name}: posted {key(item)}")
    log(f"{name}: {len(items)} releases, {len(to_post)} to post")


def main():
    if not COMPOSER_WEBHOOK_URL and not JAILBREAK_WEBHOOK_URL:
        sys.exit("set COMPOSER_WEBHOOK_URL and/or JAILBREAK_WEBHOOK_URL")
    sources = [
        (
            "composer",
            COMPOSER_WEBHOOK_URL,
            composer_releases,
            lambda r: r["version"],
            composer_embed,
        ),
        (
            "jailbreak",
            JAILBREAK_WEBHOOK_URL,
            jailbreak_releases,
            lambda r: r["tag_name"],
            jailbreak_embed,
        ),
    ]
    sources = [s for s in sources if s[1]]
    state = load_state()
    while True:
        for source in sources:
            check(state, *source)
        if ONCE:
            return
        time.sleep(POLL_INTERVAL + random.uniform(0, POLL_INTERVAL / 10))


if __name__ == "__main__":
    main()
