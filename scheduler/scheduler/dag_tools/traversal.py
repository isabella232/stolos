from collections import defaultdict
import networkx as nx

from ds_commons.util import crossproduct, flatmap_with_kwargs

from scheduler.exceptions import (
    _log_raise, _log_raise_if, DAGMisconfigured, InvalidJobId)

from .build import build_dag
from .constants import DEPENDENCY_GROUP_DEFAULT_NAME
from .node import (get_tasks_dct, parse_job_id, get_job_id_template)
from . import log


def topological_sort(lst):
    """Given a list of (app_name, job_id) pairs,
    topological sort by the app_names

    This is useful for sorting the parents and children of a node if the node
    has complex dependencies
    """
    dct = defaultdict(list)
    for app_job in lst:
        dct[app_job[0]].append(app_job)
    for node in nx.topological_sort(build_dag()):
        for app_job2 in dct[node]:
            yield app_job2


def get_parents(app_name, job_id, include_dependency_group=False,
                filter_deps=(), _filter_parents=()):
    """Return an iterator over all parent (app_name, job_id) pairs
    Given a child app_name and job_id

    `include_dependency_group` - (bool) If True,
        yield (app_name, job_id, dependency_group_name) tuples instead
    `filter_deps` - (list|tuple) only yield parents from a particular
        dependency group

    """
    build_dag()  # run validations
    if job_id:
        parsed_job_id = parse_job_id(app_name, job_id)
        filter_deps = set(filter_deps)
        if 'dependency_group_name' in parsed_job_id:
            filter_deps.add(parsed_job_id['dependency_group_name'], )

    ld = dict(  # log details
        app_name=app_name, job_id=job_id)
    for group_name, dep_group in _get_grps(app_name, filter_deps, ld):
        if not _match_group_to_job_id(app_name, job_id, dep_group, ld):
            log.debug(
                'ignoring dependency group in call to get_parents',
                extra=dict(dependency_group_name=group_name, **ld))
            continue

        kwargs = dict(
            group_name=group_name, app_name=app_name, job_id=job_id, ld=ld,
            include_dependency_group=include_dependency_group
        )
        if isinstance(dep_group, list):
            gen = _get_parents_handle_subgroups(
                group_name, dep_group, _filter_parents, ld, kwargs)
            for rv in gen:
                yield rv
        else:
            _get_parents_validate_app_name(
                app_names=dep_group['app_name'], ld=ld,
                _filter_parents=_filter_parents)
            for rv in _get_parents(dep_group=dep_group,
                                   _filter_parents=_filter_parents,
                                   **kwargs):
                yield rv


def _match_group_to_job_id(app_name, job_id, dep_group, ld):
    if isinstance(dep_group, list):
        for subgrp in dep_group:
            v = _match_group_to_job_id(app_name, job_id, subgrp, ld)
            if not v:
                return False
        return True

    pjob_id = parse_job_id(app_name, job_id)
    if 'job_id' in dep_group or len(dep_group) == 1:
        return True  # any parent job_id matches

    for key, value in pjob_id.items():
        if key == 'dependency_group_name':
            continue

        if any(value != x for x in dep_group[key]):
            return False
    return True


def _get_parents_handle_subgroups(
        group_name, dep_group, _filter_parents, ld, kwargs):

    if _filter_parents:
        # validate filtered parents are a subset of parents
        all_app_names = set(
            y for x in dep_group for y in x['app_name'])
        _get_parents_validate_app_name(
            app_names=all_app_names, ld=ld,
            _filter_parents=_filter_parents)

    for subgrp in dep_group:
        if _filter_parents:
            app_names = set(_filter_parents).intersection(subgrp['app_name'])
            if not app_names:
                continue
        else:
            app_names = subgrp['app_name']
        for rv in _get_parents(
                dep_group=subgrp, _filter_parents=app_names, **kwargs):
            yield rv


def _get_grps(app_name, filter_deps, ld):
    """
    Return an iterator that yields (dependency_group_name, group_metadata)
    tuples
    """
    td = get_tasks_dct()
    try:
        depends_on = td[app_name]['depends_on']
    except KeyError:
        return []  # this task has no dependencies
    if "app_name" in depends_on:
        grps = [(DEPENDENCY_GROUP_DEFAULT_NAME, depends_on.copy())]
        _get_parents_validate_group_names(
            [DEPENDENCY_GROUP_DEFAULT_NAME], filter_deps, ld)
    elif filter_deps:
        _get_parents_validate_group_names(
            depends_on, filter_deps, ld)
        grps = (data for data in depends_on.items()
                if data[0] in filter_deps)
    else:
        grps = depends_on.items()
    return grps


def _get_parents_validate_group_names(
        dep_names, filter_deps, ld):
    _log_raise_if(
        not set(dep_names).issuperset(filter_deps),
        "You specified dependency group names that don't exist",
        extra=dict(filter_deps=filter_deps, **ld),
        exception_kls=DAGMisconfigured)


def _get_parents_validate_app_name(app_names, ld, _filter_parents):
    """
    Ensure that we aren't trying to filter parent app_names with values
    that cannot exist in a particular dependency group
    """
    t = _filter_parents
    _log_raise_if(
        not set(app_names).issuperset(t),
        ("Misconfigured code.  You identified parents"
         " to a child that aren't this child's parents!"),
        extra=dict(
            known_parents=str(app_names),
            requested_parents=str(t),
            **ld),
        exception_kls=DAGMisconfigured)


def _get_parents(group_name, dep_group, app_name, job_id, ld,
                 include_dependency_group,
                 _filter_parents):
    """
    Handle some optional kwargs to get_parents.

    We're given a dependency group or a dependency subgroup as a dict.
    Safely modify the dict so it only contains relevant query terms defined
    by the `_filter_parents` option
    """
    if _filter_parents:
        dep_group = dep_group.copy()  # shallow copy to change the keys
        dep_group['app_name'] = _filter_parents

    for rv in _get_parent_job_ids(
            group_name, dep_group,
            child_app_name=app_name, child_job_id=job_id, ld=ld):
        if include_dependency_group:
            yield rv + (group_name, )
        else:
            yield rv


def _get_parent_job_ids(group_name, dep_group,
                        child_app_name, child_job_id, ld):
    """
    Yield the parent app_name and derived job_id for each parent listed in
    dep_group metadata

    If there is extra job_id criteria that doesn't apply to a
    particular parent app's job_id template, ignore it.
    """
    for parent_app_name in dep_group['app_name']:
        dep_group = dep_group.copy()  # shallow copy to change the keys

        if len(dep_group) == 1:
            _inject_job_id(
                dep_group, child_app_name, child_job_id, parent_app_name, ld)
        # are there specific job_ids the child would inherit from?
        if 'job_id' in dep_group:
            for rv in _iter_job_ids(dep_group=dep_group, group_name=group_name,
                                    parent_app_name=parent_app_name, ld=ld):
                yield rv
        else:
            # try to fill in the parent's job_id template and yield it
            template, parsed_template = get_job_id_template(parent_app_name)
            so_far = set()
            for job_id_data in crossproduct([dep_group[_key]
                                            for _key in parsed_template]):
                _pjob_id = dict(zip(parsed_template, job_id_data))
                parent_job_id = template.format(
                    dependency_group_name=group_name, **_pjob_id)
                if parent_job_id not in so_far:
                    so_far.add(parent_job_id)
                    yield (parent_app_name, parent_job_id)


def _inject_job_id(dep_group, child_app_name, child_job_id,
                   parent_app_name, ld):
    """Given metadata about a dependency group, set the dep_group['job_id']
    value.  Assume the dependency group only specifies an app_name key"""
    # if only "app_name" is defined in this dependency group,
    # assume child inherited the parent's job_id and passed that
    # to this child
    if child_job_id is None:
        _log_raise(
            ("It's impossible to get all parent job_ids if the"
                " child expects to inherit the parent's job_id and you"
                " haven't specified the child's job_id"),
            extra=dict(parent_app_name=parent_app_name, **ld),
            exception_kls=DAGMisconfigured)
    t, pt = get_job_id_template(parent_app_name)
    meta = parse_job_id(child_app_name, child_job_id)
    try:
        dep_group['job_id'] = [t.format(**meta)]
    except Exception as err:
        _log_raise(
            ("The child job_id doesn't contain enough metadata to"
                " create the parent job_id. Err details: %s") % err,
            extra=dict(job_id_template=t, metadata=str(meta), **ld),
            exception_kls=err.__class__)


def _iter_job_ids(dep_group, group_name, parent_app_name, ld):
    """
    Assume there specific job_ids listed in dependency group metadata that
    the child would inherit from and yield those.
    """
    for jid in dep_group['job_id']:
        try:
            parse_job_id(parent_app_name, jid)
        except InvalidJobId:
            _ld = dict(**ld)
            _ld.update(
                dependency_group_name=group_name,
                job_id=jid)
            _log_raise(
                ("There's no way parent could have the child's job_id"),
                extra=_ld,
                exception_kls=InvalidJobId)
        yield (parent_app_name, jid)


def get_children(node, job_id, include_dependency_group=True):
    dg = build_dag()
    child_apps = ((k, vv) for k, v in dg.succ[node].items() for vv in v)
    for child, group_name in child_apps:
        grp = dg.node[child]['depends_on']
        if group_name != DEPENDENCY_GROUP_DEFAULT_NAME:
            grp = grp[group_name]
        kwargs = dict(
            func=_generate_job_ids, kwarg_name='grp', list_or_value=grp,
            node=node, job_id=job_id, child=child, group_name=group_name)
        for rv in flatmap_with_kwargs(**kwargs):
            if include_dependency_group:
                yield rv + (group_name, )
            else:
                yield rv


def _generate_job_ids(node, job_id, child, group_name, grp):
    # ignore dependency groups that have nothing to do with the parent node
    if node not in grp['app_name']:
        return []

    if len(grp) == 1:
        return [(child, job_id)]

    # check that the job_id applies to this group
    pjob_id = parse_job_id(node, job_id)
    template, parsed_template = get_job_id_template(child)

    if 'job_id' in grp:
        if job_id in grp['job_id']:
            kwargs = dict()
            kwargs.update(pjob_id)
            kwargs.update({k: v[0] for k, v in grp.items() if len(v) == 1})
            cjob_id = template.format(**kwargs)
            return [(child, cjob_id)]
        return []

    for k, v in pjob_id.items():
        if k not in grp:
            return []
        if v not in grp[k]:
            return []
    return _generate_job_ids2(
        grp, parsed_template, template, group_name, child)


def _generate_job_ids2(grp, parsed_template, template, group_name, child):
    so_far = set()
    for job_id_data in crossproduct([grp[_key] for _key in parsed_template]):
        cjob_id = template.format(
            dependency_group_name=group_name,
            **dict(zip(parsed_template, job_id_data)))
        if cjob_id not in so_far:
            so_far.add(cjob_id)
            yield (child, cjob_id)
