#!/usr/bin/env python3
import argparse
import json
import sys
import traceback

import requests

# https://www.nicehash.com/doc-api
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('api_id')
    parser.add_argument('api_key')
    parser.add_argument('bitcoin_address')
    args = parser.parse_args()
    worker_stats = []
    # noinspection PyBroadException
    try:
        response = requests.get('https://api.nicehash.com/api',
                                params={"method": "balance",
                                        "id": args.api_id,
                                        "key": args.api_key})
        response.raise_for_status()
        data = response.json()
        if not data['result'].get('error'):
            response = requests.get('https://blockchain.info/ticker')
            response.raise_for_status()
            ticker_data = response.json()
            json.dump({
                'address': args.bitcoin_address,
                'bitcoin_value': float(ticker_data['USD']['last']),
                'confirmed_balance': float(data['result']['balance_confirmed']),
                'pending_balance': float(data['result']['balance_pending']),
            }, sys.stdout)
        else:
            sys.stderr.write(data['result']['error'])
        sys.exit(0)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
