import asyncio
import functools
import re
import sys
import time

import click
from kasa import Credentials, DeviceConfig, Discover
from kasa.device_factory import get_device_class_from_sys_info
from kasa.exceptions import AuthenticationError
from kasa.protocols import IotProtocol
from kasa.transports import KlapTransportV2

GET_SYSINFO_QUERY = {"system": {"get_sysinfo": {}}}


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
        "{}={:f}".format(field, field_value)
        if isinstance(field_value, float)
        else "{}={}".format(field, field_value)
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


async def connect_klap_v2(host, username, password):
    # python-kasa can't select KLAP v2 auth for IOT-family devices on its
    # own (some, like the HS300, need it); build the connection by hand.
    config = DeviceConfig(
        host=host,
        credentials=Credentials(username=username, password=password),
        timeout=5,
    )
    protocol = IotProtocol(transport=KlapTransportV2(config=config))
    info = await protocol.query(GET_SYSINFO_QUERY)
    device_class = get_device_class_from_sys_info(info)
    device = device_class(host=host, protocol=protocol)
    await device.update()
    return device


async def connect(host, username, password, klap_v2):
    if klap_v2:
        return await connect_klap_v2(host, username, password)
    try:
        device = await Discover.discover_single(
            host, username=username, password=password, timeout=5
        )
        await device.update()
        return device
    except AuthenticationError:
        if not (username and password):
            raise
        return await connect_klap_v2(host, username, password)


def energy_fields(outlet):
    energy = outlet.modules.get("Energy")
    if energy is None:
        return None
    status = energy.status
    return {
        "voltage_v": status.voltage,
        "power_w": status.power,
        "current_a": status.current,
        "total_kwh": status.total,
    }


def outlet_line(measurement_name, parent, outlet, extra_tags=None):
    fields = energy_fields(outlet)
    if fields is None:
        return None
    return to_line_protocol(
        measurement_name=measurement_name,
        tags={
            **parent.hw_info,
            "model": parent.model,
            "deviceId": outlet.device_id,
            "alias": outlet.alias,
            "state": "on" if outlet.is_on else "off",
            **(extra_tags or {}),
        },
        fields={
            **fields,
            **parent.location,
            "rssi": parent.rssi,
            "relay_state": 1 if outlet.is_on else 0,
            "on_time_s": (outlet.sys_info or parent.sys_info).get("on_time"),
        },
    )


@click.command()
@click.option(
    "--host",
    help="The host name or IP address of the TP-Link Plug to connect to.",
    required=True,
)
@click.option(
    "--username",
    envvar="TPLINK_CLOUD_USERNAME",
    default=None,
    help="TP-Link cloud account email, needed for KLAP devices (e.g. HS300, EP40).",
)
@click.option(
    "--password",
    envvar="TPLINK_CLOUD_PASSWORD",
    default=None,
    help="TP-Link cloud account password.",
)
@click.option(
    "--klap-v2",
    is_flag=True,
    help="Skip straight to KLAP v2 auth (e.g. for a known HS300) instead of "
    "trying normally first and falling back on failure.",
)
@click.pass_context
@async_command
async def cli(ctx, host, username, password, klap_v2):
    try:
        p = await connect(host, username, password, klap_v2)
    except Exception as e:
        click.echo(f"{host}: {e}", err=True)
        ctx.exit(1)

    measurement_name = "tplink_plug_stats"

    outlets = p.children if p.children else [p]

    lines = []
    for outlet in outlets:
        extra_tags = {"parent_alias": p.alias} if p.children else None
        line = outlet_line(measurement_name, p, outlet, extra_tags)
        if line is not None:
            lines.append(line)

    if not lines:
        click.echo(f"{p.model} ({p.alias}) has no outlets with energy monitoring", err=True)
        ctx.exit(1)

    for line in lines:
        click.echo(line, file=sys.stdout)


if __name__ == "__main__":
    cli()
