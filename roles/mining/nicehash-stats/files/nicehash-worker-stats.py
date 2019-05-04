#!/usr/bin/env python

import argparse
import json
import sys
import traceback
import requests


# https://www.nicehash.com/doc-api
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('bitcoin_address')
    args = parser.parse_args()
    worker_stats = []
    try:
        response = requests.get('https://blockchain.info/ticker')
        response.raise_for_status()
        bitcoin_value = float(response.json()['USD']['last'])
        response = requests.get('https://api.nicehash.com/api?method=stats.provider.ex&addr={}'.format(args.bitcoin_address))
        response.raise_for_status()
        data = response.json()
        if not data['result'].get('error'):
            for alg_stats in data['result']['current']:
                alg_speed = alg_stats['data'][0] if alg_stats.get('data') else []
                alg_speed = alg_stats['data'][0] if alg_stats.get('data') else []
                worker_stats.append({
                    'address': args.bitcoin_address,
                    'accepted': float(alg_speed.get('a')),
                    'rejected_target': float(alg_speed.get('rt', 0)),
                    'rejected_stale': float(alg_speed.get('rs', 0)),
                    'rejected_duplicate': float(alg_speed.get('rd', 0)),
                    'rejected_other': float(alg_speed.get('ro', 0)),
                    'unpaid_balance': float(alg_stats['data'][1] if len(alg_stats.get('data') or []) > 1 else 0),
                    'algo': int(alg_stats['algo']),
                    'profitability': float(alg_stats['profitability']),
                    'bitcoin_value': bitcoin_value,
                })
            json.dump(worker_stats, sys.stdout)
            sys.exit(0)
        else:
            sys.stderr.write(data['result']['error'])
            sys.exit(1)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
