from scheduler import testing_tools as tt
import nose.tools as nt
from scheduler.configuration_backend.json_config import (
    JSONMapping, JSONSequence)


def with_setup(func):

    def setup_func():
        raw = {'a': 1, 'b': [1, 2, 3], 'c': {'aa': 1, 'bb': [1, 2, 3]}}
        td = JSONMapping(raw)
        return [], dict(td=td, raw=raw)

    def teardown_func():
        pass

    return tt.with_setup(setup_func, teardown_func, True)(func)


@with_setup
def test_get_item(td, raw):
    nt.assert_is_instance(td, JSONMapping)
    nt.assert_equal(td['a'], 1)
    nt.assert_equal(td['b'], JSONSequence(raw['b']))
    nt.assert_equal(td['b'][0], 1)
    nt.assert_equal(td['c'], JSONMapping(raw['c']))
    nt.assert_equal(td['c']['bb'][-1], 3)


@with_setup
def test_set_item(td, raw):
    with nt.assert_raises(TypeError):
        td['d'] = 1