#!/usr/bin/env python3
import sys
import time
from contextlib import closing

import click
import websocket

COMMANDS = ["histo", "hwinfo", "meminfo", "pool", "scanlog", "summary", "threads"]


def get_api_data(ccminer_api_url, command):
    data = []
    with closing(websocket.WebSocket()) as ws:
        ws.connect("ws://{}/{}".format(ccminer_api_url, command))
        for sample in ws.recv().split("|"):
            if not sample:
                continue
            data.append(dict([x.lower().split("=") for x in sample.split(";")]))
    return data


def to_line_protocol(measurement_name, tags, fields, ts=None):
    tags = ",".join("{}={}".format(tag, tag_value) for tag, tag_value in tags.items())
    fields = ",".join("{}={}".format(field, field_value) for field, field_value in fields.items())
    ts = int(round(float(ts or time.time()) * 1e9))
    return "{},{} {} {}".format(measurement_name, tags, fields, ts)


@click.group()
@click.option("--url", help="ccminer api url.")
@click.pass_context
def cli(ctx, url):
    ctx.obj = ctx.obj or {}
    ctx.obj["ccminer_api_url"] = url


@cli.command()
@click.pass_context
def summary(ctx):
    try:
        data = get_api_data(ctx.obj["ccminer_api_url"], "summary")[-1]
        click.echo(
            to_line_protocol(
                measurement_name="ccminer.summary",
                tags={k: data[k] for k in ["algo", "api", "name", "ver"]},
                fields={
                    k: data[k]
                    for k in [
                        "acc",
                        "accmn",
                        "diff",
                        "gpus",
                        "khs",
                        "netkhs",
                        "pools",
                        "rej",
                        "solv",
                        "uptime",
                        "wait",
                    ]
                },
                ts=data["ts"],
            ),
            file=sys.stdout,
        )
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


@cli.command()
@click.pass_context
def pool(ctx):
    try:
        data = get_api_data(ctx.obj["ccminer_api_url"], "pool")[-1]
        click.echo(
            to_line_protocol(
                measurement_name="ccminer.pool",
                tags={k: data[k] for k in ["algo", "pool", "url", "user"]},
                fields={
                    k: data[k]
                    for k in [
                        "acc",
                        "best",
                        "diff",
                        "disco",
                        "h",
                        "last",
                        "ping",
                        "rej",
                        "solv",
                        "stale",
                        "uptime",
                        "wait",
                    ]
                },
            ),
            file=sys.stdout,
        )
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


if __name__ == "__main__":
    cli()
