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


async def connect(host, username, password):
    """Connect to a plug/strip, working around a python-kasa gap.

    python-kasa hardcodes the IOT-schema + KLAP-encryption combination to the
    v1 (MD5) auth hash, but some legacy Kasa devices on newer firmware
    (confirmed on an HS300 strip) are actually provisioned with the v2 (SHA)
    hash used by SMART-schema devices - the same mismatch the production
    Control4 driver's klap.lua documents as two selectable hash flavors.
    python-kasa has no public way to select v2 for an IOT-family device, so
    on an auth failure we retry by hand: query get_sysinfo directly over a
    v2 KLAP transport, then let python-kasa classify the resulting sysinfo
    the same way its own connect() does.
    """
    try:
        device = await Discover.discover_single(
            host, username=username, password=password, timeout=5
        )
        # discover_single() doesn't always raise AuthenticationError itself -
        # for some devices the failed handshake only surfaces once update()
        # forces the authenticated query, so both calls need to be inside
        # this same try so the except below actually catches it.
        await device.update()
        return device
    except AuthenticationError:
        if not (username and password):
            raise
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


def energy_fields(outlet):
    """Pull voltage/power/current/total off an outlet's Energy module, if it has one."""
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
    help="TP-Link cloud account email. Only needed for newer devices on the KLAP "
    "transport (e.g. HS300, EP40); legacy devices ignore it. Reads "
    "TPLINK_CLOUD_USERNAME if unset.",
)
@click.option(
    "--password",
    envvar="TPLINK_CLOUD_PASSWORD",
    default=None,
    help="TP-Link cloud account password. See --username. Reads " "TPLINK_CLOUD_PASSWORD if unset.",
)
@click.pass_context
@async_command
async def cli(ctx, host, username, password):
    try:
        p = await connect(host, username, password)
    except Exception as e:
        click.echo(f"{host}: {e}", err=True)
        ctx.exit(1)

    measurement_name = "tplink_plug_stats"

    # Multi-outlet devices (HS300, EP40, ...) expose each socket as a child
    # device with its own Energy module; single-outlet devices have none and
    # the parent device itself is the outlet.
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
