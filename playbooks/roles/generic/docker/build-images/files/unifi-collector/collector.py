"""Poll the UniFi Network API and write metrics to InfluxDB 3.

Counters the controller exposes as lifetime totals (bytes, packets, errors,
drops) are converted to per-second rates against the previous sample so
dashboards can select them directly instead of computing deltas in SQL.

Measurements and their tags:
  unifi_gw      site        gateway cpu/mem, client counts, WAN throughput
  unifi_wan     wan         the controller's 24h rolling latency/availability
                            aggregate, sampled on the EVENTS_INTERVAL cadence
                            since it is not an instantaneous reading; an
                            actual uplink transition lands in unifi_events
  unifi_device  device,type switch/AP cpu, mem, uptime, satisfaction
  unifi_port    switch,port,name  rates, link speed, PoE draw, error totals
  unifi_radio   ap,band     channel, utilization, tx power, client count
  unifi_client  client,mac,type   signal, negotiated rates, satisfaction
  unifi_events  key[,client]      event counts per poll window, plus a
                            _heartbeat row every poll so a quiet window
                            reads differently from a missed one

Every measurement carries at least one tag: InfluxDB 3 drops rows from
tagless tables when the write-ahead log is persisted to parquet.
"""

import os
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Addresses and credentials are required rather than defaulted: the image
# carries no site-specific values, so the stack definition is the only place
# that describes where this deployment points. Values are stripped because
# ansible-vault encrypt_string keeps the trailing newline of whatever was
# typed, and a newline in a header value makes requests reject the call.
UNIFI_URL = os.environ["UNIFI_URL"].strip().rstrip("/")
UNIFI_API_KEY = os.environ["UNIFI_API_KEY"].strip()
INFLUX_URL = os.environ["INFLUX_URL"].strip().rstrip("/")
INFLUX_TOKEN = os.environ["INFLUX_TOKEN"].strip()
INFLUX_DB = os.environ["INFLUX_DB"].strip()

# Tuning knobs keep defaults so the stack only declares them to override.
UNIFI_SITE = os.environ.get("UNIFI_SITE", "default").strip()
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
EVENTS_INTERVAL = int(os.environ.get("EVENTS_INTERVAL", "300"))
# The controller keeps 5-minute samples for 24 hours; asking for more just
# returns the same window.
BACKFILL_HOURS = int(os.environ.get("BACKFILL_HOURS", "24"))

API = f"{UNIFI_URL}/proxy/network/api/s/{UNIFI_SITE}"
API_V2 = f"{UNIFI_URL}/proxy/network/v2/api/site/{UNIFI_SITE}"

session = requests.Session()
session.verify = False
session.headers["X-API-KEY"] = UNIFI_API_KEY

PORT_RATES = {
    "rx_bytes": "rx_bps",
    "tx_bytes": "tx_bps",
    "rx_packets": "rx_pps",
    "tx_packets": "tx_pps",
    "rx_errors": "rx_error_rate",
    "tx_errors": "tx_error_rate",
    "rx_dropped": "rx_drop_rate",
    "tx_dropped": "tx_drop_rate",
    "rx_multicast": "rx_mcast_pps",
    "tx_multicast": "tx_mcast_pps",
}
BYTE_COUNTERS = ("rx_bytes", "tx_bytes")


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


def escape_tag(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(" ", "\\ ")
        .replace("=", "\\=")
        .replace("\n", " ")
    )


def format_fields(fields):
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            parts.append(f"{key}={'true' if value else 'false'}")
        elif isinstance(value, int):
            parts.append(f"{key}={value}i")
        elif isinstance(value, float):
            parts.append(f"{key}={value}")
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'{key}="{escaped}"')
    return ",".join(parts)


def line(measurement, tags, fields, timestamp_ns=None):
    field_text = format_fields(fields)
    if not field_text:
        return None
    tag_text = "".join(f",{k}={escape_tag(v)}" for k, v in tags.items() if v not in (None, ""))
    stamp = f" {timestamp_ns}" if timestamp_ns else ""
    return f"{measurement}{tag_text} {field_text}{stamp}"


def influx_write(lines):
    lines = [entry for entry in lines if entry]
    if not lines:
        return
    response = requests.post(
        f"{INFLUX_URL}/api/v3/write_lp",
        params={"db": INFLUX_DB, "precision": "nanosecond"},
        headers={"Authorization": f"Bearer {INFLUX_TOKEN}"},
        data="\n".join(lines).encode(),
        timeout=15,
    )
    if response.status_code >= 300:
        log(f"influx write failed {response.status_code}: {response.text[:300]}")


def api_get(path):
    response = session.get(f"{API}/{path}", timeout=20)
    response.raise_for_status()
    return response.json().get("data", [])


def api_post(url, body):
    response = session.post(url, json=body, timeout=20)
    response.raise_for_status()
    return response.json().get("data", [])


def collect_health(lines):
    for subsystem in api_get("stat/health"):
        if subsystem.get("subsystem") != "wan":
            continue
        stats = subsystem.get("gw_system-stats") or {}
        lines.append(
            line(
                "unifi_gw",
                {"site": UNIFI_SITE},
                {
                    "cpu": float(stats.get("cpu") or 0),
                    "mem": float(stats.get("mem") or 0),
                    "clients": int(subsystem.get("num_sta") or 0),
                    "wan_tx_bps": int(subsystem.get("tx_bytes-r") or 0) * 8,
                    "wan_rx_bps": int(subsystem.get("rx_bytes-r") or 0) * 8,
                },
            )
        )


def collect_wan_health(lines):
    """Emit the controller's 24-hour rolling WAN aggregate.

    uptime_stats carries its own time_period (86400 seconds): a single event
    moves it and it stays moved for the following day, so it is sampled on
    the EVENTS_INTERVAL cadence rather than every poll, and the fields are
    named for what they are rather than implying an instantaneous reading.
    """
    for subsystem in api_get("stat/health"):
        if subsystem.get("subsystem") != "wan":
            continue
        for wan_name, monitor in (subsystem.get("uptime_stats") or {}).items():
            lines.append(
                line(
                    "unifi_wan",
                    {"wan": wan_name},
                    {
                        "latency_avg_24h": int(monitor.get("latency_average") or 0),
                        "availability_24h": float(monitor.get("availability") or 0),
                    },
                )
            )


def collect_devices(lines, previous, now):
    for device in api_get("stat/device"):
        if device.get("state") != 1:
            continue
        name = device.get("name") or device.get("mac", "unknown")
        device_type = device.get("type", "unknown")
        stats = device.get("system-stats") or {}
        lines.append(
            line(
                "unifi_device",
                {"device": name, "type": device_type},
                {
                    "cpu": float(stats.get("cpu") or 0),
                    "mem": float(stats.get("mem") or 0),
                    "uptime": int(device.get("uptime") or 0),
                    "satisfaction": int(device.get("satisfaction") or 0),
                },
            )
        )

        for port in device.get("port_table") or []:
            index = port.get("port_idx")
            if not port.get("up") or index is None:
                continue
            # A few port_table entries (Shelter UPS among them) carry only the
            # instantaneous bytes-r fields with no lifetime byte counters at
            # all, which would otherwise read as a port permanently idle
            # rather than one that simply isn't measured.
            if "rx_bytes" not in port or "tx_bytes" not in port:
                continue
            key = (device.get("mac"), index)
            counters = {name_: int(port.get(name_) or 0) for name_ in PORT_RATES}
            fields = {
                "speed": int(port.get("speed") or 0),
                "poe_w": float(port.get("poe_power") or 0),
                # Lifetime totals: the rates above show what is happening now,
                # these show what a cable has done since the switch booted.
                # Kept for both directions of both error and drop counters.
                "rx_errors_total": counters["rx_errors"],
                "tx_errors_total": counters["tx_errors"],
                "rx_dropped_total": counters["rx_dropped"],
                "tx_dropped_total": counters["tx_dropped"],
            }
            # Each port carries the loop's start time as an approximation of
            # when its counters were read (jitter is at most however long
            # collect_health took earlier in the same cycle), so a rate spans
            # the gap that actually elapsed, even when a poll was skipped by a
            # failed request.
            sampled_at, earlier = previous.get(key, (None, None))
            if earlier and now > sampled_at:
                elapsed = now - sampled_at
                for counter, field in PORT_RATES.items():
                    # Clamps a counter reset (switch reboot) to zero instead of
                    # emitting a negative spike.
                    delta = max(0, counters[counter] - earlier[counter])
                    rate = delta / elapsed
                    fields[field] = rate * 8 if counter in BYTE_COUNTERS else rate
            previous[key] = (now, counters)
            lines.append(
                line(
                    "unifi_port",
                    {"switch": name, "port": index, "name": port.get("name") or f"p{index}"},
                    fields,
                )
            )

        if device_type == "uap":
            for radio in device.get("radio_table_stats") or []:
                lines.append(
                    line(
                        "unifi_radio",
                        {"ap": name, "band": radio.get("radio", "unknown")},
                        {
                            "channel": int(radio.get("channel") or 0),
                            "util": int(radio.get("cu_total") or 0),
                            "util_self_rx": int(radio.get("cu_self_rx") or 0),
                            "util_self_tx": int(radio.get("cu_self_tx") or 0),
                            "tx_power": int(radio.get("tx_power") or 0),
                            "clients": int(radio.get("user-num_sta") or 0),
                        },
                    )
                )


def collect_clients(lines):
    wired = 0
    wireless = 0
    for client in api_get("stat/sta"):
        if not isinstance(client, dict):
            continue
        tags = {
            "client": client.get("name") or client.get("hostname") or client.get("mac", "unknown"),
            "mac": client.get("mac", "unknown"),
        }
        if client.get("is_wired"):
            wired += 1
            fields = {"wired_rate_mbps": int(client.get("wired_rate_mbps") or 0)}
            tags["type"] = "wired"
        else:
            wireless += 1
            fields = {
                "rssi": int(client.get("signal") or 0),
                "tx_rate": int(client.get("tx_rate") or 0),
                "rx_rate": int(client.get("rx_rate") or 0),
                "satisfaction": int(client.get("satisfaction") or 0),
            }
            tags["type"] = "wireless"
        lines.append(line("unifi_client", tags, fields))
    lines.append(
        line(
            "unifi_gw",
            {"site": UNIFI_SITE},
            {"wired_clients": wired, "wireless_clients": wireless},
        )
    )


def collect_events(lines, window_seconds):
    now_ms = int(time.time() * 1000)
    events = api_post(
        f"{API_V2}/system-log/all",
        {
            "timestampFrom": now_ms - window_seconds * 1000,
            "timestampTo": now_ms,
            "pageNumber": 0,
            "pageSize": 500,
        },
    )
    events = events or []
    if len(events) >= 500:
        log(
            "collect events: hit the 500-row page size, some events in this window were not counted"
        )

    counts = {}
    for event in events:
        key = event.get("key", "unknown")
        # Roams are the one event worth attributing per client: a single device
        # ping-ponging between APs is the signature of a coverage problem.
        client = ""
        if key == "CLIENT_ROAMED_2":
            client = ((event.get("parameters") or {}).get("CLIENT") or {}).get("name", "unknown")
        counts[(key, client)] = counts.get((key, client), 0) + 1

    # Written every successful poll, event or not, so a quiet window and a
    # missed poll are distinguishable on a graph instead of both being a gap.
    # Field is "polls", not "count": a different name from the per-key rows
    # below so a bare sum(count) can't silently double itself against this
    # sentinel series.
    lines.append(line("unifi_events", {"key": "_heartbeat"}, {"polls": len(events)}))
    for (key, client), count in counts.items():
        tags = {"key": key}
        if client:
            tags["client"] = client
        lines.append(line("unifi_events", tags, {"count": count}))


def backfill():
    """Seed history from the controller's own retained samples.

    Without this a fresh container starts with an empty dashboard. Points are
    written at their original timestamps, so re-running on restart overwrites
    rather than duplicates.
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - BACKFILL_HOURS * 3600 * 1000
    lines = []
    try:
        site_report = api_post(
            f"{API}/stat/report/5minutes.site",
            {
                "attrs": ["time", "wan-tx_bytes", "wan-rx_bytes", "num_sta"],
                "start": start_ms,
                "end": now_ms,
            },
        )
        for row in site_report:
            stamp = row.get("time")
            if not stamp:
                continue
            lines.append(
                line(
                    "unifi_gw",
                    {"site": UNIFI_SITE},
                    {
                        "wan_tx_bps": int(row.get("wan-tx_bytes") or 0) * 8 // 300,
                        "wan_rx_bps": int(row.get("wan-rx_bytes") or 0) * 8 // 300,
                        "clients": int(row.get("num_sta") or 0),
                    },
                    timestamp_ns=int(stamp) * 1_000_000,
                )
            )
        gateway_report = api_post(
            f"{API}/stat/report/5minutes.gw",
            {"attrs": ["time", "cpu", "mem"], "start": start_ms, "end": now_ms},
        )
        for row in gateway_report:
            stamp = row.get("time")
            if not stamp or row.get("cpu") is None:
                continue
            lines.append(
                line(
                    "unifi_gw",
                    {"site": UNIFI_SITE},
                    {"cpu": float(row["cpu"]), "mem": float(row.get("mem") or 0)},
                    timestamp_ns=int(stamp) * 1_000_000,
                )
            )
        influx_write(lines)
        log(f"backfill wrote {len(lines)} points covering {BACKFILL_HOURS}h")
    except Exception as error:
        log(f"backfill skipped: {error}")


def main():
    log(f"collecting {UNIFI_URL} -> {INFLUX_URL}/{INFLUX_DB} every {POLL_INTERVAL}s")
    backfill()
    previous_ports = {}
    last_events = 0.0
    last_wan_health = 0.0
    while True:
        started = time.time()
        lines = []
        # One failing endpoint costs a single sample of one measurement; the
        # controller returns a sporadic 500 on stat/sta under load.
        for name, collector, args in (
            ("health", collect_health, (lines,)),
            ("devices", collect_devices, (lines, previous_ports, started)),
            ("clients", collect_clients, (lines,)),
        ):
            try:
                collector(*args)
            except Exception as error:
                log(f"collect {name} failed: {error}")

        if started - last_wan_health >= EVENTS_INTERVAL:
            try:
                collect_wan_health(lines)
                last_wan_health = started
            except Exception as error:
                log(f"collect wan health failed: {error}")

        if started - last_events >= EVENTS_INTERVAL:
            window = int(started - last_events) if last_events else EVENTS_INTERVAL
            try:
                collect_events(lines, window)
                # Advanced only on success so a failed poll retries its window
                # instead of dropping the events inside it.
                last_events = started
            except Exception as error:
                log(f"collect events failed: {error}")

        try:
            influx_write(lines)
        except Exception as error:
            log(f"influx write failed: {error}")

        time.sleep(max(1, POLL_INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    main()
