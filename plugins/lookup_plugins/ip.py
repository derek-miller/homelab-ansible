# From: http://stackoverflow.com/questions/32324120/arbitrary-host-name-resolution-in-ansible
import ansible.errors as errors
from ansible.plugins.lookup import LookupBase
import socket


class LookupModule(LookupBase):

    def __init__(self, basedir=None, **kwargs):
        self.basedir = basedir
        super().__init__(**kwargs)

    def run(self, terms, variables=None, **kwargs):

        hostname = terms[0]

        if not isinstance(hostname, str):
            raise errors.AnsibleError("ip lookup expects a string (hostname)")

        # noinspection PyBroadException
        try:
            resolved = socket.gethostbyname(hostname)
        except:
            resolved = '127.0.0.1'
        return [resolved]
