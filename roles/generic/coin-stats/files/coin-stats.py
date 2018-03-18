from __future__ import print_function

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
@click.argument('coinmarketcap_ids', nargs=-1, required=True)
@click.pass_context
def cli(ctx, coinmarketcap_ids):
    try:
        coinmarketcap = Market()
        for coinmarketcap_id in coinmarketcap_ids:
            data = coinmarketcap.ticker(coinmarketcap_id, convert='USD')
            if isinstance(data, list):
                data = data[-1]
            assert isinstance(data, dict)
            if 'error' in data:
                raise Exception(data['error'].replace('id not found', '{} not found'.format(coinmarketcap_id)))

            click.echo(to_line_protocol(measurement_name='coin_stats',
                                        tags={
                                            'coin': data['id'].replace('vivo', 'vivocoin'),
                                        },
                                        fields={
                                            'available_supply': data['available_supply'],
                                            'market_cap': data['market_cap_usd'],
                                            'price': data['price_usd'],
                                            'rank': data['rank'],
                                            'total_supply': data['total_supply'],
                                        }), file=sys.stdout)
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


if __name__ == '__main__':
    cli()
