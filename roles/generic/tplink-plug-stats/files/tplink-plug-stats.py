import asyncio
import functools
import re
import sys
import time

import click
from kasa import SmartPlug


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


def async_command(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


@click.command()
@click.option(
    "--host",
    help="The host name or IP address of the TP-Link Plug to connect to.",
    required=True,
)
@click.pass_context
@async_command
async def cli(ctx, host):
    p = SmartPlug(host)
    await p.update()
    if not p.is_plug:
        click.echo(f"{p.model} ({p.alias}) is not a smart plug", err=True)
        ctx.exit(1)
    if not p.has_emeter:
        click.echo(
            f"{p.model} {p.alias} does not have energy monitoring functionality",
            err=True,
        )
        ctx.exit(1)
    click.echo(
        to_line_protocol(
            measurement_name="tplink_plug_stats",
            tags={
                **p.hw_info,
                "model": p.model,
                "deviceId": p.device_id,
                "alias": p.alias,
                "state": "on" if p.is_on else "off",
            },
            fields={
                "voltage_v": p.emeter_realtime["voltage"],
                "power_w": p.emeter_realtime["power"],
                "current_a": p.emeter_realtime["current"],
                "total_kwh": p.emeter_realtime["total"],
                **p.location,
                "rssi": p.rssi,
                "relay_state": 1 if p.is_on else 0,
                "on_time_s": p.sys_info["on_time"],
            },
        ),
        file=sys.stdout,
    )


if __name__ == "__main__":
    cli()
