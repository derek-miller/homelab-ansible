# From: http://stackoverflow.com/questions/32324120/arbitrary-host-name-resolution-in-ansible
import socket

import ansible.errors as errors
from ansible.module_utils.six import string_types
from ansible.plugins.lookup import LookupBase


class LookupModule(LookupBase):
    def __init__(self, basedir=None, **kwargs):
        super().__init__(**kwargs)
        self.basedir = basedir

    def run(self, terms, variables=None, **kwargs):

        hostname = terms[0]

        if not isinstance(hostname, string_types):
            raise errors.AnsibleError("ip lookup expects a string (hostname)")

        # noinspection PyBroadException
        try:
            resolved = socket.gethostbyname(hostname)
        except:
            resolved = "127.0.0.1"
        return [resolved]
