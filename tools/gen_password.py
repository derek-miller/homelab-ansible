#!/usr/bin/env python2
import base64
import os

while True:
    p = base64.b64encode(os.urandom(72))
    if '+' not in p and '/' not in p:
        print p
        break
