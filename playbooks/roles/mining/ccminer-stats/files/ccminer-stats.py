from __future__ import print_function

import sys
import time
from contextlib import closing

import click
import websocket


COMMANDS = [
    'histo',
    'hwinfo',
    'meminfo',
    'pool',
    'scanlog',
    'summary',
    'threads',
]


def get_api_data(ccminer_api_url, command):
    data = []
    with closing(websocket.WebSocket()) as ws:
        ws.connect("ws://{}/{}".format(ccminer_api_url, command))
        for sample in ws.recv().split('|'):
            if not sample:
                continue
            data.append(dict([x.lower().split('=') for x in sample.split(';')]))
    return data


def to_line_protocol(data, measurement_name, tags, fields, ts_key=None):
    tags = ','.join('{}={}'.format(tag, data[tag]) for tag in tags)
    fields = ','.join('{}={}'.format(field, data[field]) for field in fields)
    ts = int(round((float(data[ts_key]) if ts_key else time.time()) * 1e9))
    return '{},{} {} {}'.format(measurement_name, tags, fields, ts)


@click.group()
@click.option('--url', help='ccminer api url.')
@click.option('--coin', help='coin being mined.')
@click.pass_context
def cli(ctx, url, coin):
    ctx.obj = ctx.obj or {}
    ctx.obj['ccminer_api_url'] = url
    ctx.obj['coin_name'] = coin


@cli.command()
@click.pass_context
def summary(ctx):
    try:
        data = get_api_data(ctx.obj['ccminer_api_url'], 'summary')[-1]
        data['coin'] = ctx.obj['coin_name']
        click.echo(to_line_protocol(data=data,
                                    measurement_name='ccminer.summary',
                                    tags=['algo', 'api', 'name', 'ver', 'coin'],
                                    fields=['acc', 'accmn', 'diff', 'gpus', 'khs', 'netkhs', 'pools', 'rej', 'solv',
                                            'uptime', 'wait'],
                                    ts_key='ts'), file=sys.stdout)
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


@cli.command()
@click.pass_context
def pool(ctx):
    try:
        data = get_api_data(ctx.obj['ccminer_api_url'], 'pool')[-1]
        data['coin'] = ctx.obj['coin_name']
        click.echo(to_line_protocol(data=data,
                                    measurement_name='ccminer.pool',
                                    tags=['algo', 'pool', 'url', 'user', 'coin'],
                                    fields=['acc', 'best', 'diff', 'disco', 'h', 'last', 'ping', 'rej', 'solv',
                                            'stale', 'uptime', 'wait']),
                   file=sys.stdout)
    except Exception as e:
        click.echo(str(e), err=True)
        ctx.exit(1)


if __name__ == '__main__':
    cli()
