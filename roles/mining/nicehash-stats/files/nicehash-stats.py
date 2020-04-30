#!/usr/bin/env python3
import datetime
import hashlib
import hmac
import json
import sys
import time
import uuid

import click
import requests

CHARSET = "ISO-8859-1"
NICEHASH_HOST = "api2.nicehash.com"


def nicehash_request(api_key, api_secret, org_id, method, path, auth=True, **kwargs):
    if auth:
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
        method, f"https://{NICEHASH_HOST.rstrip('/')}/{(path or '').lstrip('/')}", **kwargs
    )


def get_nicehash_algorithm_stats(api_key, api_secret, org_id):
    response = nicehash_request(
        api_key, api_secret, org_id, "get", "/main/api/v2/mining/algorithms"
    )
    response.raise_for_status()
    algorithms = {a["order"]: a for a in response.json().get("miningAlgorithms", {})}

    response = nicehash_request(
        api_key, api_secret, org_id, "get", "/main/api/v2/public/stats/global/current", auth=False
    )
    response.raise_for_status()
    for algorithm_stats in response.json().get("algos", {}):
        if algorithm_stats.get("a") not in algorithms:
            continue
        algorithms[algorithm_stats["a"]]["pricing"] = algorithm_stats["p"]
    return {a["algorithm"]: a for a in algorithms.values()}


def get_nicehash_exchange_rates(api_key, api_secret, org_id):
    response = nicehash_request(
        api_key, api_secret, org_id, "get", "/main/api/v2/exchangeRate/list"
    )
    response.raise_for_status()
    return {
        (e["fromCurrency"], e["toCurrency"]): float(e["exchangeRate"])
        for e in response.json().get("list", [])
    }


def to_line_protocol(measurement_name, tags, fields, ts=None):
    tags = ",".join("{}={}".format(tag, tag_value) for tag, tag_value in tags.items() if tag_value)
    fields = ",".join("{}={}".format(field, field_value) for field, field_value in fields.items())
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
@click.pass_context
def rig(ctx):
    try:
        response = nicehash_request(
            ctx.obj["api_key"],
            ctx.obj["api_secret"],
            ctx.obj["org_id"],
            "get",
            "/main/api/v2/mining/rigs2/",
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
        for mining_rig in data.get("miningRigs", []):
            rig_status = mining_rig.get("minerStatus", "OFFLINE")
            rig_type = mining_rig.get("type", "UNMANAGED")
            rig_id = mining_rig.get("rigId", "__DEFAULT__")
            rig_name = mining_rig.get("name", "")
            click.echo(
                to_line_protocol(
                    measurement_name="nicehash.rig",
                    tags={
                        "address": bitcoin_mining_address,
                        "status": rig_status.lower(),
                        "type": rig_type.lower(),
                        "rig_id": rig_id.lower(),
                        "name": rig_name.lower(),
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
                            "address": bitcoin_mining_address,
                            "status": rig_status.lower(),
                            "type": rig_type.lower(),
                            "rig_id": rig_id.lower(),
                            "name": rig_name.lower(),
                            "algo": algorithm.lower(),
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
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


@cli.command()
@click.pass_context
def wallet(ctx):
    try:
        response = nicehash_request(
            ctx.obj["api_key"],
            ctx.obj["api_secret"],
            ctx.obj["org_id"],
            "get",
            "/main/api/v2/accounting/accounts2",
        )
        response.raise_for_status()
        data = response.json()

        exchange_rates = get_nicehash_exchange_rates(
            ctx.obj["api_key"], ctx.obj["api_secret"], ctx.obj["org_id"]
        )

        for account in data.get("currencies") or []:
            click.echo(
                to_line_protocol(
                    measurement_name="nicehash.wallet",
                    tags={"currency": account["currency"]},
                    fields={
                        "exchange_rate": exchange_rates[(account["currency"], "USD")],
                        "balance": float(account["totalBalance"]),
                    },
                ),
                file=sys.stdout,
            )
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


if __name__ == "__main__":
    cli()
