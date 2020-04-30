#!/usr/bin/env python3
import sys
import time

import click
import requests


def to_line_protocol(measurement_name, tags, fields, ts=None):
    tags = ",".join("{}={}".format(tag, tag_value) for tag, tag_value in tags.items())
    fields = ",".join("{}={}".format(field, field_value) for field, field_value in fields.items())
    ts = int(round(float(ts or time.time()) * 1e9))
    return "{},{} {} {}".format(measurement_name, tags, fields, ts)


@click.command()
@click.option("--api-key", help="Coinmarketcap api key.", required=True)
@click.argument("coinmarketcap_slugs", nargs=-1, required=True)
@click.pass_context
def cli(ctx, api_key, coinmarketcap_slugs):
    try:
        response = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            params={"limit": "5000"},
            headers={
                "Accepts": "application/json",
                "X-CMC_PRO_API_KEY": api_key,
            },
        )
        response.raise_for_status()
        for listing in response.json()["data"]:
            if listing["slug"] not in coinmarketcap_slugs:
                continue
            click.echo(
                to_line_protocol(
                    measurement_name="coin_stats",
                    tags={
                        "slug": listing["slug"],
                        "coin": listing["name"].lower(),
                        "symbol": listing["symbol"],
                    },
                    fields={
                        "total_supply": listing["total_supply"],
                        "circulating_supply": listing["circulating_supply"],
                        "rank": listing["cmc_rank"],
                        "market_cap": listing["quote"]["USD"]["market_cap"],
                        "percent_change_1h": listing["quote"]["USD"]["percent_change_1h"],
                        "percent_change_24h": listing["quote"]["USD"]["percent_change_24h"],
                        "percent_change_7d": listing["quote"]["USD"]["percent_change_7d"],
                        "price": listing["quote"]["USD"]["price"],
                        "volume_24h": listing["quote"]["USD"]["volume_24h"],
                    },
                ),
                file=sys.stdout,
            )
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


if __name__ == "__main__":
    cli()
