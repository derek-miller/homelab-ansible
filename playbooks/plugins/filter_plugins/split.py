from ansible import errors
from ansible.module_utils.six import string_types


def split_string(string, separator=" "):
    if not isinstance(string, string_types):
        raise errors.AnsibleFilterError("|split expects a string, got " + repr(string))
    return string.split(separator)


class FilterModule(object):
    """A filter to split a string into a list."""

    def filters(self):
        return {
            "split": split_string,
        }
