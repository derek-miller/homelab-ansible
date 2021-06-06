#!/usr/bin/env python3
import base64
import os

while True:
    p = base64.b64encode(os.urandom(72)).decode()
    if "+" not in p and "/" not in p:
        print(p)
        break
