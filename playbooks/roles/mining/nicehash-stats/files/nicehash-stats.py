import datetime
import hashlib
import hmac
import json
import locale
import re
import sys
import time
import traceback
import uuid

import click
import requests
from tabulate import tabulate, tabulate_formats

locale.setlocale(locale.LC_ALL, "")

CHARSET = "ISO-8859-1"
NICEHASH_HOST = "api2.nicehash.com"


def nicehash_request(method, path, api_key=None, api_secret=None, org_id=None, **kwargs):
    if api_key and api_secret and org_id:
        time_ms = str(
            int(round(datetime.datetime.now(tz=datetime.timezone.utc).timestamp() * 1000))
        )
        nonce = str(uuid.uuid4())

        signature = hmac.new(api_secret.encode(), digestmod=hashlib.sha256)
        segments = [
            api_key.encode(CHARSET),
            time_ms.encode(CHARSET),
            nonce.encode(CHARSET),
            None,  # Unused
            org_id.encode(CHARSET),
            None,  # Unused
            method.upper().encode(CHARSET),
            path.encode(CHARSET) if path else None,
            "&".join(f"{k}={v}" for k, v in kwargs["params"].items()).encode(CHARSET)
            if kwargs.get("params")
            else None,
        ]
        if kwargs.get("json"):
            segments.append(json.dumps(kwargs["json"], sort_keys=True).encode(CHARSET))
        if kwargs.get("data"):
            segments.append(kwargs["data"].encode(CHARSET))

        signature.update(segments[0])
        for segment in segments[1:]:
            signature.update(b"\x00")
            if segment is not None:
                signature.update(segment)

        kwargs.setdefault("headers", {}).update(
            {
                "X-Time": time_ms,
                "X-Nonce": nonce,
                "X-Organization-Id": org_id,
                "X-Auth": f"{api_key}:{signature.hexdigest()}",
            }
        )
    kwargs.setdefault("headers", {}).update({"X-Request-Id": str(uuid.uuid4())})
    return requests.request(
        method,
        f"https://{NICEHASH_HOST.rstrip('/')}/{(path or '').lstrip('/')}",
        **kwargs,
    )


def get_nicehash_algorithm_stats(api_key, api_secret, org_id):
    response = nicehash_request(
        "get",
        "/main/api/v2/mining/algorithms",
        api_key=api_key,
        api_secret=api_secret,
        org_id=org_id,
    )
    response.raise_for_status()
    algorithms = {a["order"]: a for a in response.json().get("miningAlgorithms", {})}

    response = nicehash_request(
        "get",
        "/main/api/v2/public/stats/global/current",
    )
    response.raise_for_status()
    for algorithm_stats in response.json().get("algos", {}):
        if algorithm_stats.get("a") not in algorithms:
            continue
        algorithms[algorithm_stats["a"]]["pricing"] = algorithm_stats["p"]
    return {a["algorithm"]: a for a in algorithms.values()}


def get_nicehash_exchange_rates(api_key, api_secret, org_id):
    response = nicehash_request(
        "get",
        "/main/api/v2/exchangeRate/list",
        api_key=api_key,
        api_secret=api_secret,
        org_id=org_id,
    )
    response.raise_for_status()
    return {
        (e["fromCurrency"], e["toCurrency"]): float(e["exchangeRate"])
        for e in response.json().get("list", [])
    }


def to_line_protocol(measurement_name, tags, fields, ts=None):
    tags = ",".join(
        "{}={}".format(
            tag,
            re.sub(r"\s+", r"\ ", tag_value.strip()) if isinstance(tag_value, str) else tag_value,
        )
        for tag, tag_value in tags.items()
        if tag_value is not None
    )
    fields = ",".join(
        "{}={}".format(field, field_value)
        for field, field_value in fields.items()
        if field_value is not None
    )
    ts = int(round(float(ts or time.time()) * 1e9))
    return "{},{} {} {}".format(measurement_name, tags, fields, ts)


@click.group()
@click.option("--api-key", help="nicehash api key.", required=True)
@click.option("--api-secret", help="nicehash api secret.", required=True)
@click.option("--org-id", help="nicehash org id.", required=True)
@click.pass_context
def cli(ctx, api_key, api_secret, org_id):
    ctx.obj = ctx.obj or {}
    ctx.obj["api_key"] = api_key
    ctx.obj["api_secret"] = api_secret
    ctx.obj["org_id"] = org_id


@cli.command()
@click.option("--rig-id", help="nicehash miner rig id.", required=True)
@click.pass_context
def rig(ctx, rig_id):
    try:
        response = nicehash_request(
            "get",
            f"/main/api/v2/mining/rigs2/",
            api_key=ctx.obj["api_key"],
            api_secret=ctx.obj["api_secret"],
            org_id=ctx.obj["org_id"],
        )
        response.raise_for_status()
        data = response.json()

        bitcoin_value = get_nicehash_exchange_rates(
            ctx.obj["api_key"], ctx.obj["api_secret"], ctx.obj["org_id"]
        )[("BTC", "USD")]

        algorithm_stats = get_nicehash_algorithm_stats(
            ctx.obj["api_key"], ctx.obj["api_secret"], ctx.obj["org_id"]
        )
        bitcoin_mining_address = data["btcAddress"]

        # Older versions of ethminer do not correctly set the name when connected to nicehash.
        # If there is just one default rig override it with the given id.
        if (
            len(data.get("miningRigs", [])) == 1
            and data["miningRigs"][0].get("rigId") == "__DEFAULT__"
        ):
            data["miningRigs"][0]["rigId"] = rig_id

        for mining_rig in data.get("miningRigs", []):
            if rig_id != mining_rig.get("rigId"):
                continue
            rig_status = mining_rig.get("minerStatus", "OFFLINE")
            rig_type = mining_rig.get("type", "UNMANAGED")
            rig_name = mining_rig.get("name", "")
            click.echo(
                to_line_protocol(
                    measurement_name="nicehash.rig",
                    tags={
                        "address": bitcoin_mining_address or None,
                        "status": rig_status.lower() or None,
                        "type": rig_type.lower() or None,
                        "rig_id": rig_id.lower() or None,
                        "name": rig_name.lower() or None,
                    },
                    fields={
                        "bitcoin_value": bitcoin_value,
                        "unpaid_balance": float(mining_rig.get("unpaidAmount", 0)),
                    },
                    ts=float(mining_rig.get("statusTime", 0)) / 1000,
                ),
                file=sys.stdout,
            )

            for rig_stat in mining_rig.get("stats", []):
                algorithm = rig_stat.get("algorithm", {}).get("enumName", "")
                if not algorithm:
                    continue

                speed_accepted = float(rig_stat.get("speedAccepted", 0))
                pricing = float(algorithm_stats.get(algorithm, {}).get("pricing", 0))
                market_factor = float(algorithm_stats.get(algorithm, {}).get("marketFactor", 0))
                mining_factor = float(algorithm_stats.get(algorithm, {}).get("miningFactor", 0))
                bitcoin_per_day = (
                    speed_accepted * mining_factor / market_factor * 1e-8 * pricing * market_factor
                )

                click.echo(
                    to_line_protocol(
                        measurement_name="nicehash.rig_stat",
                        tags={
                            "address": bitcoin_mining_address or None,
                            "status": rig_status.lower() or None,
                            "type": rig_type.lower() or None,
                            "rig_id": rig_id.lower() or None,
                            "name": rig_name.lower() or None,
                            "algo": algorithm.lower() or None,
                        },
                        fields={
                            "bitcoin_value": bitcoin_value,
                            "pricing": pricing,
                            "market_factor": market_factor,
                            "mining_factor": mining_factor,
                            "bitcoin_per_day": bitcoin_per_day,
                            "unpaid_balance": float(rig_stat.get("unpaidAmount", 0)),
                            "difficulty": float(rig_stat.get("difficulty", 0)),
                            "uptime_ms": float(rig_stat.get("timeConnected", 0)),
                            "accepted": speed_accepted,
                            "rejected_target": float(rig_stat.get("speedRejectedR1Target", 0)),
                            "rejected_stale": float(rig_stat.get("speedRejectedR2Stale", 0)),
                            "rejected_duplicate": float(
                                rig_stat.get("speedRejectedR3Duplicate", 0)
                            ),
                            "rejected_time": float(rig_stat.get("speedRejectedR4NTime", 0)),
                            "rejected_other": float(rig_stat.get("speedRejectedR5Other", 0)),
                            "rejected_total": float(rig_stat.get("speedRejectedTotal", 0)),
                        },
                        ts=float(rig_stat.get("statsTime", 0)) / 1000,
                    ),
                    file=sys.stdout,
                )
    except Exception:
        click.echo(traceback.format_exc(), err=True)
        ctx.exit(1)


@cli.command()
@click.pass_context
def wallet(ctx):
    try:
        response = nicehash_request(
            "get",
            "/main/api/v2/accounting/accounts2",
            api_key=ctx.obj["api_key"],
            api_secret=ctx.obj["api_secret"],
            org_id=ctx.obj["org_id"],
        )
        response.raise_for_status()
        data = response.json()

        exchange_rates = get_nicehash_exchange_rates(
            ctx.obj["api_key"], ctx.obj["api_secret"], ctx.obj["org_id"]
        )
        total = data.get("total") or {}
        if total:
            click.echo(
                to_line_protocol(
                    measurement_name="nicehash.wallet_total",
                    tags={"currency": total["currency"]},
                    fields={
                        "exchange_rate": exchange_rates[(total["currency"], "USD")],
                        "balance": float(total["totalBalance"]),
                        "available": float(total["available"]),
                        "pending": float(total["pending"]),
                    },
                ),
                file=sys.stdout,
            )

        for account in data.get("currencies") or []:
            exchange_rate_key = (account["currency"], "USD")
            if exchange_rate_key not in exchange_rates:
                continue
            click.echo(
                to_line_protocol(
                    measurement_name="nicehash.wallet",
                    tags={"currency": account["currency"]},
                    fields={
                        "exchange_rate": exchange_rates[exchange_rate_key],
                        "balance": float(account["totalBalance"]),
                    },
                ),
                file=sys.stdout,
            )
    except Exception:
        click.echo(traceback.format_exc(), err=True)
        ctx.exit(1)


@cli.command()
@click.pass_context
def market(ctx):
    response = nicehash_request(
        "get",
        "/exchange/api/v2/info/marketStats",
        api_key=ctx.obj["api_key"],
        api_secret=ctx.obj["api_secret"],
        org_id=ctx.obj["org_id"],
    )
    response.raise_for_status()
    for market_symbol, stats in response.json().items():
        if not market_symbol.endswith("USDT"):
            continue
        symbol, _ = market_symbol.split("USDT", 1)

        price_stat = stats["csjs"][-1]
        click.echo(
            to_line_protocol(
                measurement_name="nicehash.market",
                tags={"symbol": symbol},
                fields={
                    "price": float(price_stat["v"]),
                    "lowest_price_in_24h": float(stats["l24"]),
                    "highest_price_in_24h": float(stats["h24"]),
                    "volume_in_btc_in_24h": float(stats["v24"]),
                    "volume_in_base_cur_in_24h": float(stats["v24b"]),
                    "volume_in_quote_cur_in_24h": float(stats["v24q"]),
                    "trades_in_24h": float(stats["t24"]),
                    "percent_change_in_24h": stats["c24"] * 100.0,
                },
                ts=price_stat["d"],
            ),
            file=sys.stdout,
        )


class ClickNicehashAlgorithmChoice(click.Choice):
    name = "nicehash-algorithm-choice"

    def __init__(self):
        super().__init__([], case_sensitive=False)
        self._known_algorithms = None

    def convert(self, value, param, ctx):
        if not self.choices:
            response = nicehash_request(
                "get",
                "/main/api/v2/mining/algorithms",
            )
            response.raise_for_status()
            self.choices = sorted({a["algorithm"] for a in response.json()["miningAlgorithms"]})
        return super().convert(value, param, ctx)


class ClickRegex(click.ParamType):
    name = "regex"

    def __init__(self, as_bytes: bool = False, flags: int = 0):
        self._as_bytes = as_bytes
        self._flags = flags

    def convert(self, value, param, ctx):
        try:
            return re.compile(value.encode("utf-8") if self._as_bytes else value, flags=self._flags)
        except re.error as e:
            self.fail(str(e), param, ctx)


@cli.command()
@click.option(
    "-c",
    "--category",
    "categories",
    type=click.Choice(["GPU", "CPU", "ASIC"], case_sensitive=False),
    multiple=True,
)
@click.option(
    "-a",
    "--algorithm",
    "algorithms",
    type=ClickNicehashAlgorithmChoice(),
    multiple=True,
)
@click.option(
    "-d",
    "--device-regex",
    type=ClickRegex(flags=re.IGNORECASE),
)
@click.option("-k", "--kwh-cost", default=0.1, type=click.FLOAT)
@click.option(
    "-s",
    "--sort-by",
    type=click.Choice(["profitability", "profit_per_day", "revenue_per_day"], case_sensitive=False),
    default="profitability",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(exists=False, dir_okay=False, file_okay=True, allow_dash=True, writable=True),
    default="-",
)
@click.option(
    "-f",
    "--output-format",
    type=click.Choice(["csv"] + tabulate_formats),
    default="plain",
)
@click.pass_context
def profitability(
    ctx, categories, algorithms, device_regex, kwh_cost, sort_by, output, output_format
):
    bitcoin_value = get_nicehash_exchange_rates(
        ctx.obj["api_key"], ctx.obj["api_secret"], ctx.obj["org_id"]
    )[("BTC", "USD")]

    response = nicehash_request(
        "get",
        "/main/api/v2/public/profcalc/devices",
    )
    response.raise_for_status()

    one_day_ago = datetime.datetime.now() - datetime.timedelta(days=1)
    device_profitability = []
    devices = (response.json() or {}).get("devices") or []
    for device in devices:
        if (
            not device.get("name")
            or (categories and device["category"] not in categories)
            or (device_regex and not device_regex.match(device["name"]))
        ):
            continue
        speeds = {
            a: s
            for a, s in json.loads(device["speeds"]).items()
            # Filter out zero/empty speeds and algorithms not asked for
            if s and float(s) and (not algorithms or a in algorithms)
        }
        for algorithm_name, speed in speeds.items():
            response = nicehash_request(
                "post",
                "/main/api/v2/public/profcalc/profitability",
                json={"speeds": {algorithm_name: speed}},
            )
            response.raise_for_status()

            algorithm_profitability = response.json()
            count = 0
            total = 0.0
            algorithm_code = algorithm_profitability["codes"][algorithm_name]
            for ts, p in sorted(
                (algorithm_profitability.get("values") or {}).items(),
                key=lambda item: item[0],
                reverse=True,
            ):
                if datetime.datetime.fromtimestamp(int(ts)) < one_day_ago:
                    break
                assert p["a"] == algorithm_code
                count += 1
                total += p["p"]

            if count == 0 or total == 0:
                continue
            bitcoin_per_day = total / count
            revenue_per_day = bitcoin_per_day * bitcoin_value
            cost_per_day = kwh_cost * (device["power"] / 1000.0 * 24)
            device_profitability.append(
                {
                    "device": device["name"],
                    "algorithm_name": algorithm_name,
                    "speed": speed,
                    "power_w": device["power"],
                    "kwh_cost": kwh_cost,
                    "power_cost_per_day": cost_per_day,
                    "bitcoin_per_day": bitcoin_per_day,
                    "revenue_per_day": revenue_per_day,
                    "profit_per_day": revenue_per_day - cost_per_day,
                    "bitcoin_value": bitcoin_value,
                    "profitability": round((revenue_per_day / cost_per_day) * 100, ndigits=2)
                    if cost_per_day
                    else float("inf"),
                }
            )
    # Sort by profitability
    device_profitability.sort(key=lambda p: p[sort_by], reverse=True)

    with sys.stdout if output == "-" else open(output, "w+") as f:
        headers = [
            "GPU",
            "Algorithm",
            "Speed",
            "Power Consumption (W)",
            "kWh Cost",
            "Power Cost / 24h",
            "BTC / 24h",
            "BTC Price",
            "Revenue / 24h",
            "Profit / 24h",
            "Profitability %",
        ]
        rows = [
            [
                str(x)
                for x in [
                    dp["device"],
                    dp["algorithm_name"],
                    dp["speed"],
                    dp["power_w"],
                    dp["kwh_cost"],
                    locale.currency(dp["power_cost_per_day"]),
                    dp["bitcoin_per_day"],
                    locale.currency(dp["bitcoin_value"]),
                    locale.currency(dp["revenue_per_day"]),
                    locale.currency(dp["profit_per_day"]),
                    dp["profitability"],
                ]
            ]
            for dp in device_profitability
        ]
        if output_format == "csv":
            click.echo(",".join(headers), file=f)
            for row in rows:
                click.echo(",".join(row), file=f)
        else:
            click.echo(
                tabulate(rows, headers=headers, tablefmt=output_format),
                file=f,
            )


if __name__ == "__main__":
    cli()
