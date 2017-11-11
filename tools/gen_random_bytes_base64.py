#!/usr/bin/env python2
import base64
import os
import sys

print base64.b64encode(os.urandom(int(sys.argv[1])))
