import logging as _logging
log = _logging.getLogger('stolos')

import os.path as _p
import pkg_resources as _pkg_resources
__version__ = _pkg_resources.get_distribution(
    _p.basename(_p.dirname(_p.abspath(__file__)))).version


class Uninitialized(Exception):
    msg = (
        "Before you use Stolos, please let it initialize by calling"
        " stolos.api.initialize()")

    def __getattr__(self, *args, **kwargs):
        raise Uninitialized(Uninitialized.msg)

    def __repr__(self):
        return "Stolos Not Initialized.  %s" % Uninitialized.msg

    def __str__(self):
        return repr(self)

__all__ = ['api']
from stolos import api
