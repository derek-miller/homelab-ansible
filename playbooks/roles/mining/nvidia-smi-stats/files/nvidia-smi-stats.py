import re
import subprocess
import sys
import time
import traceback

import click


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
        "{}={:f}".format(field, field_value) if isinstance(field_value, float) else "{}={}".format(field, field_value)
        for field, field_value in fields.items()
        if field_value is not None
    )
    ts = int(round(float(ts or time.time()) * 1e9))
    return "{},{} {} {}".format(measurement_name, tags, fields, ts)


@click.command()
@click.pass_context
def cli(ctx):
    tags = [
        "accounting.mode",
        "clocks_throttle_reasons.active",
        "clocks_throttle_reasons.active",
        "clocks_throttle_reasons.applications_clocks_setting",
        "clocks_throttle_reasons.applications_clocks_setting",
        "clocks_throttle_reasons.gpu_idle",
        "clocks_throttle_reasons.gpu_idle",
        "clocks_throttle_reasons.hw_power_brake_slowdown",
        "clocks_throttle_reasons.hw_power_brake_slowdown",
        "clocks_throttle_reasons.hw_slowdown",
        "clocks_throttle_reasons.hw_slowdown",
        "clocks_throttle_reasons.hw_thermal_slowdown",
        "clocks_throttle_reasons.hw_thermal_slowdown",
        "clocks_throttle_reasons.supported",
        "clocks_throttle_reasons.supported",
        "clocks_throttle_reasons.sw_power_cap",
        "clocks_throttle_reasons.sw_power_cap",
        "clocks_throttle_reasons.sw_thermal_slowdown",
        "clocks_throttle_reasons.sw_thermal_slowdown",
        "clocks_throttle_reasons.sync_boost",
        "clocks_throttle_reasons.sync_boost",
        "compute_mode",
        "display_active",
        "display_mode",
        "driver_version",
        "index",
        "name",
        "pci.bus",
        "pci.bus_id",
        "pci.device",
        "pci.device_id",
        "pci.domain",
        "pci.sub_device_id",
        "persistence_mode",
        "power.management",
        "pstate",
        "serial",
        "uuid",
        "vbios_version",
    ]
    fields = [
        "accounting.buffer_size",
        "clocks.current.graphics",
        "clocks.current.memory",
        "clocks.current.sm",
        "clocks.current.video",
        "clocks.max.graphics",
        "clocks.max.memory",
        "clocks.max.sm",
        "count",
        "enforced.power.limit",
        "fan.speed",
        "memory.free",
        "memory.total",
        "memory.used",
        "pcie.link.gen.current",
        "pcie.link.gen.max",
        "pcie.link.width.current",
        "pcie.link.width.max",
        "power.default_limit",
        "power.draw",
        "power.limit",
        "power.max_limit",
        "power.min_limit",
        "temperature.gpu",
        "temperature.memory",
        "utilization.gpu",
        "utilization.memory",
    ]

    # noinspection PyBroadException
    try:
        query_gpu_fields = fields + tags
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--format=noheader,nounits,csv",
                "--query-gpu={}".format(",".join(query_gpu_fields)),
            ],
            stderr=subprocess.STDOUT,
        ).decode()
        for line in output.splitlines(False):
            gpu_fields = {}
            gpu_tags = {}
            for name, value in zip(query_gpu_fields, [x.strip() for x in line.split(",")]):
                if value in ("[N/A]", "N/A"):
                    continue
                safe_name = name.replace(".", "_")
                # noinspection PyBroadException
                if name in tags:
                    gpu_tags[safe_name] = value
                else:
                    try:
                        gpu_fields[safe_name] = float(value)
                    except ValueError:
                        pass
            click.echo(
                to_line_protocol(
                    measurement_name="nvidia_smi",
                    tags=gpu_tags,
                    fields=gpu_fields,
                ),
                file=sys.stdout,
            )
    except Exception:
        click.echo(traceback.format_exc(), err=True)
        ctx.exit(1)


if __name__ == "__main__":
    cli()
