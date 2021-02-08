import json
import re
import socket
import sys
import time
from contextlib import closing

import click


def get_api_data(ethminer_api_host, ethminer_api_port, method, chunk_size=4096):
    data = ""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.connect((ethminer_api_host, ethminer_api_port))
        s.sendall(
            "{}\n".format(
                json.dumps({"id": 0, "jsonrpc": "2.0", "method": method})
            ).encode("utf-8")
        )
        while True:
            chunk = s.recv(chunk_size)
            if not chunk:
                break
            data += chunk.decode("utf-8")
            if len(chunk) < 4096 and chunk[-3:] == b"]}\n":
                break
    return json.loads(data)


def to_line_protocol(measurement_name, tags, fields, ts=None):
    tags = ",".join(
        "{}={}".format(
            tag,
            re.sub(r"\s+", r"\ ", tag_value.strip())
            if isinstance(tag_value, str)
            else tag_value,
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
@click.option("--host", help="ethminer api host.", default="localhost")
@click.option("--port", help="ethminer api port.", type=click.INT)
@click.pass_context
def cli(ctx, host, port):
    ctx.obj = ctx.obj or {}
    ctx.obj["ethminer_api_host"] = host
    ctx.obj["ethminer_api_port"] = port


@cli.command()
@click.pass_context
def stats(ctx):
    try:
        # {
        #   "id": 1,
        #   "jsonrpc": "2.0",
        #   "result": [
        #     "ethminer-0.16.0.dev0+commit.41639944",  // 0 Running ethminer's version
        #     "48",  // 1 Total running time in minutes
        #     "87221;54;0",  // 2 ETH hashrate in KH/s, submitted shares, rejected shares
        #     "14683;14508;14508;14508;14508;14508",  // 3 Detailed ETH hashrate in KH/s per GPU
        #     "0;0;0",  // 4 DCR hashrate in KH/s, submitted shares, rejected shares (not used)
        #     "off;off;off;off;off;off",  // 5 Detailed DCR hashrate in KH/s per GPU (not used)
        #     "53;90;50;90;56;90;58;90;61;90;60;90",  // 6 Temp and fan speed pairs per GPU
        #     "eu1.ethermine.org:4444",  // 7 Mining pool currently active
        #     "0;0;0;0"  // 8 ETH invalid shares, ETH pool switches, DCR invalid shares, DCR pool switches
        #   ]
        # }
        raw_data = get_api_data(
            ctx.obj["ethminer_api_host"], ctx.obj["ethminer_api_port"], "miner_getstat1"
        )
        khs, submitted_shares, rejected_shares = raw_data["result"][2].split(";")
        (
            eth_invalid_shares,
            eth_pool_switches,
            dcr_invalid_shares,
            dcr_pool_switches,
        ) = raw_data["result"][8].split(";")
        tags = {
            "algo": "daggerhashimoto",
            "version": raw_data["result"][0],
            "pool": raw_data["result"][7],
        }
        fields = {
            "uptime": float(raw_data["result"][1]) * 60,
            "khs": float(khs),
            "submitted_shares": float(submitted_shares),
            "rejected_shares": float(rejected_shares),
            "eth_invalid_shares": float(eth_invalid_shares),
            "eth_pool_switches": float(eth_pool_switches),
            "dcr_invalid_shares": float(dcr_invalid_shares),
            "dcr_pool_switches": float(dcr_pool_switches),
        }
        click.echo(
            to_line_protocol(
                measurement_name="ethminer.stats",
                tags=tags,
                fields=fields,
            ),
            file=sys.stdout,
        )
        for i, gpu_khs in enumerate(raw_data["result"][3].split(";")):
            click.echo(
                to_line_protocol(
                    measurement_name="ethminer.stats_per_gpu",
                    tags={
                        "gpu": i,
                        **tags,
                    },
                    fields={"khs": float(gpu_khs)},
                ),
                file=sys.stdout,
            )
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


if __name__ == "__main__":
    cli()
