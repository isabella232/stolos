from contextlib import contextmanager
from os.path import join
import nose
import os
import subprocess
import tempfile
import ujson

from stolos import zookeeper_tools as zkt, exceptions, dag_tools as dt
from stolos.testing_tools import configure_logging
from stolos.configuration_backend.json_config import JSONMapping


CMD = (
    'python -m stolos.runner --zookeeper_hosts localhost:2181'
    ' -a {app_name} {extra_opts}'
)
TASKS_JSON = os.environ['TASKS_JSON']
TASKS_JSON_TMPFILES = {}

zk = zkt.get_zkclient('localhost:2181')
job_id1 = '20140606_1111_profile'
job_id2 = '20140606_2222_profile'
job_id3 = '20140604_1111_profile'
app1, app2, app3, app4, depends_on1, bash1 = [None] * 6
log = None  # log is configured


def create_tasks_json(fname_suffix='', inject={}, rename=False):
    """
    Create a new default tasks.json file
    This of this like a useful setup() operation before tests.

    `inject` - (dct) (re)define new tasks to add to the tasks graph
    mark this copy as the new default.
    `fname_suffix` - suffix in the file name of the new tasks.json file
    `rename` - if True, change the name all tasks to include the fname_suffix

    """
    tasks_config = dt.get_tasks_config().cache
    tasks_config.update(inject)  # assume we're using a json config

    f = tempfile.mkstemp(prefix='tasks_json', suffix=fname_suffix)[1]
    frv = ujson.dumps(tasks_config)
    if rename:
        renames = [(k, "%s__%s" % (k, fname_suffix))
                   for k in tasks_config]
        for k, new_k in renames:
            frv = frv.replace(ujson.dumps(k), ujson.dumps(new_k))
    with open(f, 'w') as fout:
        fout.write(frv)

    os.environ['TASKS_JSON'] = f
    try:
        dt.build_dag_cached.cache.clear()
    except AttributeError:
        pass
    return f


def setup_func(func_name):
    """Code that runs just before each test and configures a tasks.json file
    for each test.  The tasks.json tmp files are stored in a global var."""
    zk.delete('test_stolos', recursive=True)

    # TODO: figure out how to make this ugly convenience hack
    # better.  maybe functions should initialize app1 = get_app_name(name)
    global app1, app2, app3, app4, depends_on1, bash1
    oapp1 = 'test_stolos/test_app'
    oapp2 = 'test_stolos/test_app2'
    app1 = '%s__%s' % (oapp1, func_name)
    app2 = '%s__%s' % (oapp2, func_name)
    app3 = 'test_stolos/test_app3__%s' % func_name
    app4 = 'test_stolos/test_app4__%s' % func_name
    depends_on1 = 'test_stolos/test_depends_on__%s' % func_name
    bash1 = 'test_stolos/test_bash__%s' % func_name

    global log
    log = configure_logging('stolos.tests.test_stolos')

    f = create_tasks_json(fname_suffix=func_name, rename=True)
    TASKS_JSON_TMPFILES[func_name] = f


def teardown_func(func_name):
    """Code that runs just after each test"""
    os.environ['TASKS_JSON'] = TASKS_JSON
    os.remove(TASKS_JSON_TMPFILES[func_name])


def with_setup(func):
    """A nose test-aware decorator that lets you specify
    setup and teardown functions that know the name of the function they
    perform setup and teardown for"""
    setup = lambda: setup_func(func.func_name)
    teardown = lambda: teardown_func(func.func_name)
    return nose.tools.with_setup(setup, teardown)(func)


@contextmanager
def _inject_into_dag(new_task_dct):
    """Update (add or replace) tasks in dag with new task config.
    This should reset any cacheing within the running Stolos instance,
    but it's not guaranteed.
    Assumes that the config we're using is the JSONMapping
    """
    f = create_tasks_json(inject=new_task_dct)
    new_task_dct = JSONMapping(new_task_dct)

    # verify injection worked
    dg = dt.get_tasks_config()
    assert isinstance(dg, JSONMapping)
    dag = dt.build_dag_cached()
    for k, v in new_task_dct.items():
        assert dg[k] == v, (
            "test code: _inject_into_dag didn't insert the new tasks?")
        assert dag.node[k] == dict(v), (
            "test code: _inject_into_dag didn't reset the dag graph")

    yield
    os.remove(f)
    dt.build_dag_cached.cache.clear()


@with_setup
def test_maybe_add_subtask_no_priority():
    zkt.maybe_add_subtask(app1, job_id1)
    zkt.maybe_add_subtask(app1, job_id2)
    nose.tools.assert_equal(consume_queue(app1), job_id1)
    nose.tools.assert_equal(consume_queue(app1), job_id2)


@with_setup
def test_maybe_add_subtask_priority_first():
    zkt.maybe_add_subtask(app1, job_id1, priority=10)
    zkt.maybe_add_subtask(app1, job_id2, priority=20)
    nose.tools.assert_equal(consume_queue(app1), job_id1)
    nose.tools.assert_equal(consume_queue(app1), job_id2)


@with_setup
def test_maybe_add_subtask_priority_second():
    zkt.maybe_add_subtask(app1, job_id1, priority=20)
    zkt.maybe_add_subtask(app1, job_id2, priority=10)
    nose.tools.assert_equal(consume_queue(app1), job_id2)
    nose.tools.assert_equal(consume_queue(app1), job_id1)


@with_setup
def test_bypass_scheduler():
    validate_zero_queued_task(bash1)
    run_code(
        bash1,
        '--bypass_scheduler --job_id %s --bash echo 123' % job_id1)
    validate_zero_queued_task(bash1)
    validate_zero_completed_task(bash1)


@with_setup
def test_no_tasks():
    """
    The script shouldn't fail if it doesn't find any queued tasks
    """
    run_code(app1)
    validate_zero_queued_task(app1)
    validate_zero_queued_task(app2)


@with_setup
def test_create_child_task_after_one_parent_completed():
    # if you modify the tasks.json file in the middle of processing the dag
    # modifications to the json file should be recognized

    # the child task should run if another parent completes
    # but otherwise should not run until it's manually queued

    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    validate_one_completed_task(app1, job_id1)

    injected_app = 'test_stolos/test_module_ch'
    dct = {
        injected_app: {
            "job_type": "bash",
            "depends_on": {"app_name": [app1, app2]},
        },
    }
    with _inject_into_dag(dct):
        validate_zero_queued_task(injected_app)
        # unnecessary side effect: app1 queues app2...
        consume_queue(app2)
        zkt.set_state(app2, job_id1, zk=zk, completed=True)

        validate_one_completed_task(app2, job_id1)
        validate_one_queued_task(injected_app, job_id1)
        run_code(injected_app, '--bash echo 123')
        validate_one_completed_task(injected_app, job_id1)


@with_setup
def test_create_parent_task_after_child_completed():
    # if you modify the tasks.json file in the middle of processing the dag
    # modifications to the json file should be recognized appropriately

    # we do not re-schedule the child unless parent is completed

    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    validate_one_completed_task(app1, job_id1)

    injected_app = 'test_stolos/test_module_ch'
    child_injapp = 'test_stolos/testX'
    dct = {
        injected_app: {
            "job_type": "bash",
        },
        child_injapp: {
            "job_type": "bash",
            "depends_on": {"app_name": [injected_app]}
        }
    }
    with _inject_into_dag(dct):
        validate_zero_queued_task(injected_app)
        zkt.set_state(injected_app, job_id1, zk=zk, completed=True)
        validate_one_completed_task(injected_app, job_id1)
        validate_one_queued_task(child_injapp, job_id1)


@with_setup
def test_should_not_add_queue_while_consuming_queue():
    """
    This test guards from doubly queuing jobs
    This protects from simultaneous operations on root and leaf nodes
    ie (parent and child) for the following operations:
    adding, readding or a mix of both
    """
    enqueue(app1, job_id1)

    q = zk.LockingQueue(app1)
    q.get()
    validate_one_queued_task(app1, job_id1)

    enqueue(app1, job_id1)
    with nose.tools.assert_raises(exceptions.JobAlreadyQueued):
        zkt.readd_subtask(app1, job_id1, zk=zk)
    validate_one_queued_task(app1, job_id1)


@with_setup
def test_push_tasks():
    """
    Child tasks should be generated and executed properly

    if task A --> task B, and we queue & run A,
    then we should end up with one A task completed and one queued B task
    """
    enqueue(app1, job_id1)
    run_code(app1)
    validate_one_completed_task(app1, job_id1)
    # check child
    validate_one_queued_task(app2, job_id1)


@with_setup
def test_rerun_pull_tasks():
    # queue and complete app 1. it queues a child
    enqueue(app1, job_id1)
    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    consume_queue(app1)
    validate_zero_queued_task(app1)
    validate_one_queued_task(app2, job_id1)
    # complete app 2
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    consume_queue(app2)
    validate_zero_queued_task(app2)

    # readd app 2
    zkt.readd_subtask(app2, job_id1, zk=zk)
    validate_zero_queued_task(app1)
    validate_one_queued_task(app2, job_id1)
    # run app 2.  the parent was previously completed
    run_code(app2)
    validate_one_completed_task(app1, job_id1)  # previously completed
    validate_one_completed_task(app2, job_id1)


@with_setup
def test_rerun_manual_task():
    enqueue(app1, job_id1)
    validate_one_queued_task(app1, job_id1)

    with nose.tools.assert_raises(exceptions.JobAlreadyQueued):
        zkt.readd_subtask(app1, job_id1, zk=zk)

    # test2: re-add function can't add tasks if they've never been added
    zk.delete('test_stolos', recursive=True)
    zkt.readd_subtask(app1, job_id1, zk=zk)
    validate_one_queued_task(app1, job_id1)


@with_setup
def test_rerun_push_tasks_when_manually_queuing_child_and_parent():
    _test_rerun_tasks_when_manually_queuing_child_and_parent()

    # complete parent first
    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    consume_queue(app1)
    validate_one_completed_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)

    # child completes normally
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    consume_queue(app2)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)


@with_setup
def test_rerun_pull_tasks_when_manually_queuing_child_and_parent():
    _test_rerun_tasks_when_manually_queuing_child_and_parent()

    # complete child first
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    consume_queue(app2)
    # --> parent still queued
    validate_one_queued_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)

    # then complete parent
    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    consume_queue(app1)
    # --> child gets re-queued
    validate_one_completed_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)

    # complete child second time
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    consume_queue(app2)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)


def _test_rerun_tasks_when_manually_queuing_child_and_parent():
    # complete parent and child
    enqueue(app1, job_id1)
    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    consume_queue(app1)
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    consume_queue(app2)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)

    # manually re-add child
    zkt.readd_subtask(app2, job_id1, zk=zk)
    validate_one_queued_task(app2, job_id1)
    validate_one_completed_task(app1, job_id1)

    # manually re-add parent
    zkt.readd_subtask(app1, job_id1, zk=zk)
    validate_one_queued_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)


@with_setup
def test_rerun_push_tasks():
    # this tests recursively deleteing parent status on child nodes

    # queue and complete app 1. it queues a child
    enqueue(app1, job_id1)
    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    consume_queue(app1)
    validate_one_completed_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)

    # complete app 2
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    consume_queue(app2)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)

    # readd app 1
    zkt.readd_subtask(app1, job_id1, zk=zk)
    validate_one_queued_task(app1, job_id1)
    validate_zero_queued_task(app2)
    nose.tools.assert_true(
        zkt.check_state(app2, job_id1, zk=zk, pending=True))

    # complete app 1
    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    consume_queue(app1)
    validate_one_completed_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)
    # complete app 2
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    consume_queue(app2)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)


@with_setup
def test_complex_dependencies_pull_push():
    job_id = '20140601_1'
    enqueue(depends_on1, job_id)
    run_code(depends_on1, '--bash echo 123')

    parents = dt.get_parents(depends_on1, job_id)
    parents = list(dt.topological_sort(parents))
    for parent, pjob_id in parents[:-1]:
        zkt.set_state(parent, pjob_id, zk=zk, completed=True)
        validate_zero_queued_task(depends_on1)
    zkt.set_state(*parents[-1], zk=zk, completed=True)
    validate_one_queued_task(depends_on1, job_id)
    run_code(depends_on1, '--bash echo 123')
    validate_one_completed_task(depends_on1, job_id)


@with_setup
def test_complex_dependencies_readd():
    job_id = '20140601_1'

    # mark everything completed
    parents = list(dt.topological_sort(dt.get_parents(depends_on1, job_id)))
    for parent, pjob_id in parents:
        zkt.set_state(parent, pjob_id, zk=zk, completed=True)
    # --> parents should queue our app
    validate_one_queued_task(depends_on1, job_id)
    consume_queue(depends_on1)
    zkt.set_state(depends_on1, job_id, zk=zk, completed=True)
    validate_one_completed_task(depends_on1, job_id)

    log.warn("OK... Now try complex dependency test with a readd")
    # re-complete the very first parent.
    # we assume that this parent is a root task
    parent, pjob_id = parents[0]
    zkt.readd_subtask(parent, pjob_id, zk=zk)
    validate_one_queued_task(parent, pjob_id)
    validate_zero_queued_task(depends_on1)
    consume_queue(parent)
    zkt.set_state(parent, pjob_id, zk=zk, completed=True)
    validate_one_completed_task(parent, pjob_id)
    # since that parent re-queues children that may be depends_on1's
    # parents, complete those too!
    for p2, pjob2 in dt.get_children(parent, pjob_id, False):
        if p2 == depends_on1:
            continue
        consume_queue(p2)
        zkt.set_state(p2, pjob2, zk=zk, completed=True)
    # now, that last parent should have queued our application
    validate_one_queued_task(depends_on1, job_id)
    run_code(depends_on1, '--bash echo 123')
    validate_one_completed_task(depends_on1, job_id)
    # phew!


@with_setup
def test_pull_tasks():
    """
    Parent tasks should be generated and executed before child tasks
    (The Bubble Up and then Bubble Down test)

    If A --> B, and:
        we queue and run B, then we should have 0 completed tasks,
        but A should be queued

        nothing should change until:
            we run A and A becomes completed
            we then run B and B becomes completed
    """
    enqueue(app2, job_id1)
    run_code(app2)
    validate_one_queued_task(app1, job_id1)
    validate_zero_queued_task(app2)

    run_code(app2)
    validate_one_queued_task(app1, job_id1)
    validate_zero_queued_task(app2)

    run_code(app1)
    validate_one_completed_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)
    run_code(app2)
    validate_one_completed_task(app2, job_id1)


@with_setup
def test_pull_tasks_with_many_children():
    enqueue(app4, job_id1)
    validate_one_queued_task(app4, job_id1)
    validate_zero_queued_task(app1)
    validate_zero_queued_task(app2)
    validate_zero_queued_task(app3)

    run_code(app4, '--bash echo app4helloworld')
    validate_zero_queued_task(app4)
    validate_one_queued_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)
    validate_one_queued_task(app3, job_id1)

    consume_queue(app1)
    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    validate_zero_queued_task(app4)
    validate_one_completed_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)
    validate_one_queued_task(app3, job_id1)

    consume_queue(app2)
    zkt.set_state(app2, job_id1, zk=zk, completed=True)
    validate_zero_queued_task(app4)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)
    validate_one_queued_task(app3, job_id1)

    consume_queue(app3)
    zkt.set_state(app3, job_id1, zk=zk, completed=True)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)
    validate_one_completed_task(app3, job_id1)
    validate_one_queued_task(app4, job_id1)

    consume_queue(app4)
    zkt.set_state(app4, job_id1, zk=zk, completed=True)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)
    validate_one_completed_task(app3, job_id1)
    validate_one_completed_task(app4, job_id1)


@with_setup
def test_retry_failed_task():
    """
    Retry failed tasks up to max num retries and then remove self from queue

    Tasks should maintain proper task state throughout.
    """
    # create 2 tasks in same queue
    enqueue(app1, job_id1)
    enqueue(app1, job_id2, validate_queued=False)
    nose.tools.assert_equal(2, get_zk_status(app1, job_id1)['app_qsize'])
    nose.tools.assert_equal(job_id1, cycle_queue(app1))
    # run job_id2 and have it fail
    run_code(app1, extra_opts='--bash "&& notacommand...fail" ')
    # ensure we still have both items in the queue
    nose.tools.assert_true(get_zk_status(app1, job_id1)['in_queue'])
    nose.tools.assert_true(get_zk_status(app1, job_id2)['in_queue'])
    # ensure the failed task is sent to back of the queue
    nose.tools.assert_equal(2, get_zk_status(app1, job_id1)['app_qsize'])
    nose.tools.assert_equal(job_id1, cycle_queue(app1))
    # run and fail n times, where n = max failures
    run_code(app1, extra_opts='--max_retry 1 --bash "&& notacommand...fail"')
    # verify that job_id2 is removed from queue
    validate_one_queued_task(app1, job_id1)
    # verify that job_id2 state is 'failed' and job_id1 is still pending
    validate_one_failed_task(app1, job_id2)


@with_setup
def test_valid_if_or():
    """Invalid tasks should be automatically completed.
    This is a valid_if_or test  (aka passes_filter )... bad naming sorry!"""
    job_id = '20140606_3333_content'
    enqueue(app2, job_id, validate_queued=False)
    validate_one_skipped_task(app2, job_id)


@with_setup
def test_valid_if_or_func():
    """Verify that the valid_if_or option supports the "_func" option

    app_name: {"valid_if_or": {"_func": "python.import.path.to.func"}}
    where the function definition looks like: func(**parsed_job_id)
    """
    zk.delete('test_stolos', recursive=True)
    enqueue(app3, job_id2, validate_queued=False)
    validate_one_skipped_task(app3, job_id2)

    zk.delete('test_stolos', recursive=True)
    enqueue(app3, job_id3, validate_queued=False)
    validate_one_skipped_task(app3, job_id3)

    # if the job_id matches the valid_if_or: {"_func": func...} criteria, then:
    zk.delete('test_stolos', recursive=True)
    enqueue(app3, job_id1, validate_queued=False)
    validate_one_queued_task(app3, job_id1)


@with_setup
def test_valid_task():
    """Valid tasks should be automatically completed"""
    job_id = '20140606_3333_profile'
    enqueue(app2, job_id)


@with_setup
def test_bash():
    """a bash task should execute properly """
    # queue task
    enqueue(bash1, job_id1)
    validate_one_queued_task(bash1, job_id1)
    # run failing task
    run_code(bash1, '--bash thiscommandshouldfail')
    validate_one_queued_task(bash1, job_id1)
    # run successful task
    run_code(bash1, '--bash echo 123')
    validate_zero_queued_task(bash1)


@with_setup
def test_app_has_command_line_params():
    enqueue(bash1, job_id1)
    msg = 'output: %s'
    # Test passed in params exist
    _, logoutput = run_code(
        bash1, extra_opts='--redirect_to_stderr --bash echo newfakereadfp',
        capture=True, raise_on_err=True)
    nose.tools.assert_in(
        'newfakereadfp', logoutput, msg % logoutput)


@with_setup
def test_run_given_specific_job_id():
    enqueue(app1, job_id1)
    out, err = run_code(
        app1, '--job_id %s' % job_id1, raise_on_err=False, capture=True)
    nose.tools.assert_regexp_matches(err, (
        'UserWarning: Will not execute this task because it might be'
        ' already queued or completed!'))
    validate_one_queued_task(app1, job_id1)


@with_setup
def test_readd_change_child_state_while_child_running():
    #
    # This test guarantees that we can readd a parent task and have child task
    # fails.  If the child directly modifies the parent's output, then you
    # still have an issue.

    # TODO
    raise nose.plugins.skip.SkipTest()


@with_setup
def test_child_running_while_parent_pending_but_not_executing():
    enqueue(app1, job_id1)
    enqueue(app2, job_id1)
    parents_completed, consume_queue, parent_locks = \
        zkt.ensure_parents_completed(app2, job_id1, zk=zk, timeout=1)
    # ensure lock is obtained by ensure_parents_completed
    validate_one_queued_executing_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)
    nose.tools.assert_equal(parents_completed, False)
    # child should promise to remove itself from queue
    nose.tools.assert_equal(consume_queue, True)
    nose.tools.assert_equal(len(parent_locks), 1)


@with_setup
def test_child_running_while_parent_pending_and_executing():
    enqueue(app1, job_id1)
    enqueue(app2, job_id1)
    lock = zkt.obtain_execute_lock(app1, job_id1, zk=zk)
    assert lock
    parents_completed, consume_queue, parent_locks = \
        zkt.ensure_parents_completed(app2, job_id1, zk=zk, timeout=1)
    validate_one_queued_executing_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)
    nose.tools.assert_equal(parents_completed, False)
    # child should not promise to remove itself from queue
    nose.tools.assert_equal(consume_queue, False)
    nose.tools.assert_equal(parent_locks, set())


@with_setup
def test_race_condition_when_parent_queues_child():
    # The parent queues the child and the child runs before the parent gets
    # a chance to mark itself as completed
    zkt.set_state(app1, job_id1, zk=zk, pending=True)
    lock = zkt.obtain_execute_lock(app1, job_id1, zk=zk)
    assert lock
    zkt._maybe_queue_children(
        parent_app_name=app1, parent_job_id=job_id1, zk=zk)
    validate_one_queued_task(app2, job_id1)
    validate_zero_queued_task(app1)

    # should not complete child.  should de-queue child
    # should not queue parent.
    # should exit gracefully
    run_code(app2)
    validate_zero_queued_task(app1)
    validate_one_queued_task(app2, job_id1)

    zkt.set_state(app1, job_id1, zk=zk, completed=True)
    lock.release()
    validate_one_completed_task(app1, job_id1)
    validate_one_queued_task(app2, job_id1)

    run_code(app2)
    validate_one_completed_task(app1, job_id1)
    validate_one_completed_task(app2, job_id1)


@with_setup
def test_run_multiple_given_specific_job_id():
    p = run_code(
        bash1,
        extra_opts='--job_id %s --timeout 1 --bash sleep 1' % job_id1,
        async=True)
    p2 = run_code(
        bash1,
        extra_opts='--job_id %s --timeout 1 --bash sleep 1' % job_id1,
        async=True)
    # one of them should fail.  both should run asynchronously
    err = p.communicate()[1] + p2.communicate()[1]
    statuses = [p.poll(), p2.poll()]
    # one job succeeds.  one job fails
    nose.tools.assert_regexp_matches(err, 'successfully completed job')
    nose.tools.assert_regexp_matches(err, (
        '(UserWarning: Will not execute this task because it might'
        ' be already queued or completed!'
        '|Lock already acquired)'))
    # failing job should NOT gracefully quit
    nose.tools.assert_equal(
        list(sorted(statuses)), [0, 1], msg="expected exactly one job to fail")


@with_setup
def test_run_failing_spark_given_specific_job_id():
    """
    task should still get queued if --job_id is specified and the task fails
    """
    with nose.tools.assert_raises(Exception):
        run_code(bash1, '--pluginfail')
    validate_zero_queued_task(bash1)
    run_code(bash1, '--job_id %s --bash kasdfkajsdfajaja' % job_id1)
    validate_one_queued_task(bash1, job_id1)


@with_setup
def test_failing_task():
    _, err = run_code(
        bash1, ' --job_id 20101010_-1_profile --bash notacommand...fail',
        capture=True)
    nose.tools.assert_regexp_matches(
        err, "Bash job failed")
    nose.tools.assert_regexp_matches(
        err, "Task retry count increased")

    _, err = run_code(bash1, '--max_retry 1 --bash jaikahhaha', capture=True)
    nose.tools.assert_regexp_matches(
        err, "Task retried too many times and is set as permanently failed")


@with_setup
def test_invalid_queued_job_id():
    job_id = '0011_i_dont_work_123_w_234'
    # manually bypass the decorator that validates job_id
    zkt._set_state_unsafe(app4, job_id, zk=zk, pending=True)
    q = zk.LockingQueue(app4)
    q.put(job_id)
    validate_one_queued_task(app4, job_id)

    run_code(app4, '--bash echo 123')
    validate_one_failed_task(app4, job_id)
    validate_zero_queued_task(app4)


def enqueue(app_name, job_id, validate_queued=True):
    # initialize job
    zkt.maybe_add_subtask(app_name, job_id, zk)
    zkt.maybe_add_subtask(app_name, job_id, zk)
    # verify initial conditions
    if validate_queued:
        validate_one_queued_task(app_name, job_id)


def run_code(app_name, extra_opts='', capture=False, raise_on_err=True,
             async=False):
    """Execute a shell command that runs Stolos for a given app_name

    `async` - (bool) return Popen process instance.  other kwargs do not apply
    `capture` - (bool) return (stdout, stderr)
    """
    cmd = CMD.format(app_name=app_name, extra_opts=extra_opts)
    log.debug('run code', extra=dict(cmd=cmd))
    p = subprocess.Popen(
        cmd, shell=True, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    if async:
        return p

    stdout, stderr = p.communicate()
    rc = p.poll()
    if raise_on_err and rc:
        raise Exception(
            "command failed. returncode: %s\ncmd: %s\nstderr: %s\nstdout: %s\n"
            % (rc, cmd, stderr, stdout))
    log.warn("Ran a shell command and got this stdout and stderr: "
             " \nSTDOUT:\n%s \nSTDERR:\n %s"
             % (stdout, stderr))
    if capture:
        return stdout, stderr


def validate_zero_queued_task(app_name):
    if zk.exists(join(app_name, 'entries')):
        nose.tools.assert_equal(
            0, len(zk.get_children(join(app_name, 'entries'))))


def validate_zero_completed_task(app_name):
    if zk.exists(join(app_name, 'all_subtasks')):
        nose.tools.assert_equal(
            0, len(zk.get_children(join(app_name, 'all_subtasks'))))


def validate_one_failed_task(app_name, job_id):
    status = get_zk_status(app_name, job_id)
    nose.tools.assert_equal(status['num_execute_locks'], 0)
    nose.tools.assert_equal(status['num_add_locks'], 0)
    nose.tools.assert_equal(status['in_queue'], False)
    # nose.tools.assert_equal(status['app_qsize'], 1)
    nose.tools.assert_equal(status['state'], 'failed')


def validate_one_queued_executing_task(app_name, job_id):
    status = get_zk_status(app_name, job_id)
    nose.tools.assert_equal(status['num_execute_locks'], 1)
    nose.tools.assert_equal(status['num_add_locks'], 0)
    nose.tools.assert_equal(status['in_queue'], True)
    nose.tools.assert_equal(status['app_qsize'], 1)
    nose.tools.assert_equal(status['state'], 'pending')


def validate_one_queued_task(app_name, job_id):
    status = get_zk_status(app_name, job_id)
    nose.tools.assert_equal(status['num_execute_locks'], 0)
    nose.tools.assert_equal(status['num_add_locks'], 0)
    nose.tools.assert_equal(status['in_queue'], True)
    nose.tools.assert_equal(status['app_qsize'], 1)
    nose.tools.assert_equal(status['state'], 'pending')


def validate_one_completed_task(app_name, job_id):
    status = get_zk_status(app_name, job_id)
    nose.tools.assert_equal(status['num_execute_locks'], 0)
    nose.tools.assert_equal(status['num_add_locks'], 0)
    nose.tools.assert_equal(status['in_queue'], False)
    nose.tools.assert_equal(status['app_qsize'], 0)
    nose.tools.assert_equal(status['state'], 'completed')


def validate_one_skipped_task(app_name, job_id):
    status = get_zk_status(app_name, job_id)
    nose.tools.assert_equal(status['num_execute_locks'], 0)
    nose.tools.assert_equal(status['num_add_locks'], 0)
    nose.tools.assert_equal(status['in_queue'], False)
    nose.tools.assert_equal(status['app_qsize'], 0)
    nose.tools.assert_equal(status['state'], 'skipped')


def cycle_queue(app_name):
    """Get item from queue, put at back of queue and return item"""
    q = zk.LockingQueue(app_name)
    item = q.get()
    q.put(item)
    q.consume()
    return item


def consume_queue(app_name, timeout=1):
    q = zk.LockingQueue(app_name)
    item = q.get(timeout=timeout)
    q.consume()
    return item


def get_zk_status(app_name, job_id):
    path = zkt._get_zookeeper_path(app_name, job_id)
    elockpath = join(path, 'execute_lock')
    alockpath = join(path, 'add_lock')
    entriespath = join(app_name, 'entries')
    return {
        'num_add_locks': len(
            zk.exists(alockpath) and zk.get_children(alockpath) or []),
        'num_execute_locks': len(
            zk.exists(elockpath) and zk.get_children(elockpath) or []),
        'in_queue': (
            any(zk.get(join(app_name, 'entries', x))[0] == job_id
                for x in zk.get_children(entriespath))
            if zk.exists(entriespath) else False),
        'app_qsize': (
            len(zk.get_children(entriespath))
            if zk.exists(entriespath) else 0),
        'state': zk.get(path)[0],
    }