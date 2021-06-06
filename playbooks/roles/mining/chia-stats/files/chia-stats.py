import re
import subprocess
import sys
import time
import traceback
from typing import Tuple, Mapping, Union

import click
from humanfriendly import parse_size, parse_timespan


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


def farming_status_to_ordinal(status: str) -> int:
    if status == "not available":
        return 0
    elif status == "farming":
        return 1
    elif status == "syncing":
        return 2
    elif status == "not synced or not connected to peers":
        return 3
    elif status == "not running":
        return 4
    return -1


def wallet_sync_status_to_ordinal(status: str) -> int:
    if status == "synced":
        return 1
    elif status == "not synced":
        return 0
    return -1


@click.command()
@click.option("--chia-exe", help="ethminer api host.", required=True)
@click.pass_context
def cli(ctx, chia_exe: str):
    # noinspection PyBroadException
    try:
        farm_summary_tags, farm_summary_fields = farm_summary_stats(chia_exe=chia_exe)
        wallet_tags, wallet_fields = wallet_stats(chia_exe=chia_exe)
    except subprocess.CalledProcessError as ex:
        click.echo(traceback.format_exc(), err=True)
        click.echo(ex.output.decode(), err=True)
        ctx.exit(1)
        return
    except Exception:
        click.echo(traceback.format_exc(), err=True)
        ctx.exit(1)
        return

    click.echo(
        to_line_protocol(
            measurement_name="chia",
            tags={
                **farm_summary_tags,
                **wallet_tags,
            },
            fields={
                **farm_summary_fields,
                **wallet_fields,
            },
        ),
        file=sys.stdout,
    )


def farm_summary_stats(chia_exe: str) -> Tuple[Mapping[str, str], Mapping[str, Union[int, float]]]:
    output = subprocess.check_output(
        [chia_exe, "farm", "summary"], stderr=subprocess.STDOUT
    ).decode()
    tags = {}
    fields = {}
    """
    Farming status: Farming
    Total chia farmed: 0.0
    User transaction fees: 0.0
    Block rewards: 0.0
    Last height farmed: 0
    Plot count: 54
    Total size of plots: 5.345 TiB
    Estimated network space: 5722.224 PiB
    Expected time to win: 6 months and 4 weeks
    Note: log into your key using 'chia wallet show' to see rewards for each key
    """
    for line in output.splitlines(False):
        if line.lower().startswith("note:"):
            continue

        match = re.match(r"Farming status: (.*)", line, flags=re.IGNORECASE)
        if match:
            status = match.group(1).strip().lower()
            tags["farming_status_name"] = status
            fields["farming_status"] = farming_status_to_ordinal(status)
            continue

        match = re.match(r"Total chia farmed: (\d+(?:\.\d+)?)", line, flags=re.IGNORECASE)
        if match:
            fields["total_chia_farmed"] = float(match.group(1))
            continue

        match = re.match(r"User transaction fees: (\d+(?:\.\d+)?)", line, flags=re.IGNORECASE)
        if match:
            fields["user_tx_fees"] = float(match.group(1))
            continue

        match = re.match(r"Block rewards: (\d+(?:\.\d+)?)", line, flags=re.IGNORECASE)
        if match:
            fields["block_rewards"] = float(match.group(1))
            continue

        match = re.match(r"Last height farmed: (\d+)", line, flags=re.IGNORECASE)
        if match:
            fields["last_height_farmed"] = int(match.group(1))
            continue

        match = re.match(r"Plot count: (\d+)", line, flags=re.IGNORECASE)
        if match:
            fields["plot_count"] = int(match.group(1))
            continue

        match = re.match(r"Total size of plots: (.*)", line, flags=re.IGNORECASE)
        if match:
            fields["total_plot_size_bytes"] = parse_size(match.group(1))
            continue

        match = re.match(r"Estimated network space: (.*)", line, flags=re.IGNORECASE)
        if match:
            fields["estimated_network_size_bytes"] = parse_size(match.group(1))
            continue

        match = re.match(r"Expected time to win: (.*)", line, flags=re.IGNORECASE)
        if match:
            seconds = 0
            for part in [p.strip() for p in match.group(1).split("and") if p]:
                month_match = re.match(r"(\d+) months?", part, flags=re.IGNORECASE)
                if month_match:
                    part = f"{int(month_match.group(1)) * 30} days"
                seconds += parse_timespan(part)
            fields["expected_time_to_win_secs"] = int(seconds)
            continue

        raise Exception(f"unknown farm summary line: {line}")

    return tags, fields


def wallet_stats(chia_exe: str) -> Tuple[Mapping[str, str], Mapping[str, Union[int, float]]]:
    output = subprocess.check_output(
        [chia_exe, "wallet", "show"], stderr=subprocess.STDOUT
    ).decode()
    tags = {}
    fields = {}
    """
    Wallet height: 290619
    Sync status: Synced
    Balances, fingerprint: 786897079
    Wallet ID 1 type STANDARD_WALLET
       -Total Balance: 0.0 xch (0 mojo)
       -Pending Total Balance: 0.0 xch (0 mojo)
       -Spendable: 0.0 xch (0 mojo)
   """
    for line in output.splitlines(False):
        match = re.match(r"Wallet ID \d+ type .*", line, flags=re.IGNORECASE)
        if match:
            continue

        match = re.match(r"Wallet height: (\d+)", line, flags=re.IGNORECASE)
        if match:
            fields["wallet_height"] = int(match.group(1))
            continue

        match = re.match(r"Sync status: (.*)", line, flags=re.IGNORECASE)
        if match:
            status = match.group(1).strip().lower()
            tags["wallet_sync_status_name"] = status
            fields["wallet_sync_status"] = wallet_sync_status_to_ordinal(status)
            continue

        match = re.match(r"Balances, fingerprint: (.*)", line, flags=re.IGNORECASE)
        if match:
            tags["fingerprint"] = match.group(1).strip()
            continue

        match = re.match(r"\s+-Total Balance: (\d+(?:\.\d+)?) xch.*", line, flags=re.IGNORECASE)
        if match:
            fields["wallet_total_balance"] = fields.get("wallet_total_balance", 0.0) + float(
                match.group(1)
            )
            continue

        match = re.match(
            r"\s+-Pending Total Balance: (\d+(?:\.\d+)?) xch.*", line, flags=re.IGNORECASE
        )
        if match:
            fields["wallet_total_pending_balance"] = fields.get(
                "wallet_total_pending_balance", 0.0
            ) + float(match.group(1))
            continue

        match = re.match(r"\s+-Spendable: (\d+(?:\.\d+)?) xch.*", line, flags=re.IGNORECASE)
        if match:
            fields["wallet_spendable_balance"] = fields.get(
                "wallet_spendable_balance", 0.0
            ) + float(match.group(1))
            continue

        raise Exception(f"unknown farm summary line: {line}")

    return tags, fields


if __name__ == "__main__":
    cli()
