"""Run `cswap auto --json` and post quarantined accounts to a Discord webhook."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import truststore

truststore.inject_into_ssl()

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
CSWAP = os.environ.get("CSWAP_BIN", "cswap")
AUTO_ARGS = json.loads(os.environ.get("CSWAP_AUTO_ARGS", "[]"))
QUIET_EVENTS = {"poll", "sleep", "no-switch"}
COLOR = 0xE67E22


def log(message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def post(event):
    embed = {
        "title": f"Account-{event['number']} needs a re-login",
        "description": (
            f"{event['email']} was quarantined ({event['reason']}). Log in with it, "
            f"then run `cswap add --slot {event['number']}`."
        ),
        "color": COLOR,
        "timestamp": event["ts"],
    }
    body = json.dumps({"embeds": [embed]}).encode()
    for _ in range(5):
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "claude-swap-notify"},
        )
        try:
            urllib.request.urlopen(req, timeout=30).close()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                # The exception's own text carries the URL, and the URL holds the token.
                log(f"discord post failed: HTTP {e.code}")
                return
            time.sleep(float(json.load(e).get("retry_after", 1)) + 0.5)
        except OSError as e:
            log(f"discord post failed: {type(e).__name__}")
            time.sleep(10)
    log("discord post failed: gave up after retries")


def main():
    proc = subprocess.Popen(
        [CSWAP, "auto", "--json", *AUTO_ARGS], stdout=subprocess.PIPE, text=True
    )
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log(line.rstrip())
            continue
        kind = event.get("event")
        if kind not in QUIET_EVENTS:
            log(line.rstrip())
        if kind == "account-quarantined":
            post(event)
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
