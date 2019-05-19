#!/usr/bin/env python3
import sys
import time

import click
from coinmarketcap import Market


def to_line_protocol(measurement_name, tags, fields):
    tags = ','.join('{}={}'.format(tag, tag_value) for tag, tag_value in tags.items())
    fields = ','.join('{}={}'.format(field, field_value) for field, field_value in fields.items())
    ts = int(round((float(time.time()) * 1e9)))
    return '{},{} {} {}'.format(measurement_name, tags, fields, ts)


@click.command()
@click.argument('coinmarketcap_slugs', nargs=-1, required=True)
@click.pass_context
def cli(ctx, coinmarketcap_slugs):
    try:
        coinmarketcap = Market()
        slug_to_id = {listing['website_slug']: listing['id'] for listing in coinmarketcap.listings()['data']}
        for coinmarketcap_slug in coinmarketcap_slugs:
            if coinmarketcap_slug not in slug_to_id:
                raise Exception('id for slug "{}" not found'.format(coinmarketcap_slug))
            data = coinmarketcap.ticker(slug_to_id[coinmarketcap_slug], convert='USD')
            if 'metadata' in data and data['metadata'].get('error'):
                raise Exception(data['metadata']['error'])
            elif 'data' in data:
                data = data['data']
            click.echo(to_line_protocol(measurement_name='coin_stats',
                                        tags={
                                            'slug': data['website_slug'],
                                            'coin': data['name'].lower().replace('vivo', 'vivocoin'),
                                            'symbol': data['symbol'],
                                        },
                                        fields={
                                            'total_supply': data['total_supply'],
                                            'circulating_supply': data['circulating_supply'],
                                            'rank': data['rank'],
                                            'market_cap': data['quotes']['USD']['market_cap'],
                                            'percent_change_1h': data['quotes']['USD']['percent_change_1h'],
                                            'percent_change_24h': data['quotes']['USD']['percent_change_24h'],
                                            'percent_change_7d': data['quotes']['USD']['percent_change_7d'],
                                            'price': data['quotes']['USD']['price'],
                                            'volume_24h': data['quotes']['USD']['volume_24h'],
                                        }), file=sys.stdout)
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


if __name__ == '__main__':
    cli()
