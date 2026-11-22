"""Serialize administration changes with the final blacklist submission gate."""
from contextlib import contextmanager
from functools import wraps
import fcntl
import os


class RequestNotSent(ValueError):
    """A local configuration gate rejected a mutation before any HTTP request."""


@contextmanager
def configuration_lock():
    path = os.environ.get('MDBLISTARR_BLACKLIST_CONFIG_LOCK_PATH', '/tmp/mdblistarr-blacklist-config.lock')
    with open(path, 'a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def configuration_write_lock(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == 'POST':
            with configuration_lock():
                return view(request, *args, **kwargs)
        return view(request, *args, **kwargs)
    return wrapped
