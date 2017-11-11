#!/usr/bin/env python

import argparse
import json
import sys
import traceback
import urllib2


# https://www.nicehash.com/doc-api
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('api_id')
    parser.add_argument('api_key')
    parser.add_argument('bitcoin_address')
    args = parser.parse_args()
    worker_stats = []
    try:
        response = urllib2.urlopen('https://api.nicehash.com/api?method=balance&id={}&key={}'.format(args.api_id, args.api_key))
        data = json.load(response)
        if not data['result'].get('error'):
            json.dump({
                'address': args.bitcoin_address,
                'bitcoin_value': float(json.load(urllib2.urlopen('https://blockchain.info/ticker'))['USD']['last']),
                'confirmed_balance': float(data['result']['balance_confirmed']),
                'pending_balance': float(data['result']['balance_pending']),
            }, sys.stdout)
        else:
            sys.stderr.write(data['result']['error'])
        sys.exit(0)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
