# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
from __future__ import absolute_import

import os
import platform

import anaconda_project.internal.conda_api as conda_api
from anaconda_project.test.environ_utils import (minimal_environ, minimal_environ_no_conda_env,
                                                 strip_environ_keeping_conda_env)
from anaconda_project.internal.test.http_utils import http_get_async, http_post_async
from anaconda_project.internal.test.tmpfile_utils import with_directory_contents
from anaconda_project.internal.test.test_conda_api import monkeypatch_conda_not_to_use_links
from anaconda_project.prepare import prepare, UI_MODE_BROWSER, UI_MODE_TEXT_ASSUME_NO
from anaconda_project.project_file import DEFAULT_PROJECT_FILENAME
from anaconda_project.project import Project
from anaconda_project.plugins.registry import PluginRegistry
from anaconda_project.plugins.providers.conda_env import CondaEnvProvider

from tornado import gen

if platform.system() == 'Windows':
    script_dir = "Scripts"
    conda_env_var = 'CONDA_DEFAULT_ENV'
else:
    script_dir = "bin"
    conda_env_var = 'CONDA_ENV_PATH'


def test_find_by_class_name_conda_env():
    registry = PluginRegistry()
    found = registry.find_provider_by_class_name(class_name="CondaEnvProvider")
    assert found is not None
    assert isinstance(found, CondaEnvProvider)


def test_prepare_project_scoped_env(monkeypatch):
    monkeypatch_conda_not_to_use_links(monkeypatch)

    def prepare_project_scoped_env(dirname):
        project = Project(dirname)
        fake_old_path = "foo" + os.pathsep + "bar"
        environ = dict(PROJECT_DIR=dirname, PATH=fake_old_path)
        result = prepare(project, environ=environ)
        assert result
        expected_env = os.path.join(dirname, "envs", "default")
        if platform.system() == 'Windows':
            expected_new_path = expected_env + os.pathsep + os.path.join(
                expected_env, script_dir) + os.pathsep + os.path.join(expected_env, "Library",
                                                                      "bin") + os.pathsep + "foo" + os.pathsep + "bar"
        else:
            expected_new_path = os.path.join(expected_env, script_dir) + os.pathsep + "foo" + os.pathsep + "bar"
        expected = dict(CONDA_ENV_PATH=expected_env,
                        CONDA_DEFAULT_ENV=expected_env,
                        PROJECT_DIR=project.directory_path,
                        PATH=expected_new_path)
        if platform.system() == 'Windows':
            del expected['CONDA_ENV_PATH']
        expected == result.environ
        assert os.path.exists(os.path.join(expected_env, "conda-meta"))
        conda_meta_mtime = os.path.getmtime(os.path.join(expected_env, "conda-meta"))

        # bare minimum default env shouldn't include these
        # (contrast with the test later where we list them in
        # requirements)
        installed = conda_api.installed(expected_env)
        assert 'ipython' not in installed
        assert 'numpy' not in installed

        # Prepare it again should no-op (use the already-existing environment)
        environ = dict(PROJECT_DIR=dirname, PATH=fake_old_path)
        result = prepare(project, environ=environ)
        assert result
        expected = dict(CONDA_ENV_PATH=expected_env,
                        CONDA_DEFAULT_ENV=expected_env,
                        PROJECT_DIR=project.directory_path,
                        PATH=expected_new_path)
        if platform.system() == 'Windows':
            del expected['CONDA_ENV_PATH']
        assert expected == result.environ
        assert conda_meta_mtime == os.path.getmtime(os.path.join(expected_env, "conda-meta"))

    with_directory_contents(dict(), prepare_project_scoped_env)


def test_prepare_project_scoped_env_conda_create_fails(monkeypatch):
    def mock_create(prefix, pkgs, channels):
        raise conda_api.CondaError("error_from_conda_create")

    monkeypatch.setattr('anaconda_project.internal.conda_api.create', mock_create)

    def prepare_project_scoped_env_fails(dirname):
        project = Project(dirname)
        environ = minimal_environ(PROJECT_DIR=dirname)
        result = prepare(project, environ=environ)
        assert not result

    with_directory_contents(dict(), prepare_project_scoped_env_fails)


def test_prepare_project_scoped_env_not_attempted_in_check_mode(monkeypatch):
    def mock_create(prefix, pkgs, channels):
        raise Exception("Should not have attempted to create env")

    monkeypatch.setattr('anaconda_project.internal.conda_api.create', mock_create)

    def prepare_project_scoped_env_not_attempted(dirname):
        project = Project(dirname)
        environ = minimal_environ(PROJECT_DIR=dirname)
        result = prepare(project, environ=environ, ui_mode=UI_MODE_TEXT_ASSUME_NO)
        assert not result
        expected_env_path = os.path.join(dirname, "envs", "default")
        assert ['missing requirement to run this project: A Conda environment',
                "  '%s' doesn't look like it contains a Conda environment yet." % expected_env_path] == result.errors

    with_directory_contents(dict(), prepare_project_scoped_env_not_attempted)


def test_prepare_project_scoped_env_with_packages(monkeypatch):
    monkeypatch_conda_not_to_use_links(monkeypatch)

    def prepare_project_scoped_env_with_packages(dirname):
        project = Project(dirname)
        environ = minimal_environ(PROJECT_DIR=dirname)
        result = prepare(project, environ=environ)
        assert result

        prefix = result.environ[conda_env_var]
        installed = conda_api.installed(prefix)

        assert 'ipython' in installed
        assert 'numpy' in installed
        assert 'ipython-notebook' not in installed

        # Preparing it again with new packages added should add those
        deps = project.project_file.get_value('dependencies')
        project.project_file.set_value('dependencies', deps + ['ipython-notebook'])
        project.project_file.save()
        environ = minimal_environ(PROJECT_DIR=dirname)
        result = prepare(project, environ=environ)
        assert result

        prefix = result.environ[conda_env_var]
        installed = conda_api.installed(prefix)

        assert 'ipython' in installed
        assert 'numpy' in installed
        assert 'ipython-notebook' in installed

        # Preparing it again with a bogus package should fail
        deps = project.project_file.get_value('dependencies')
        project.project_file.set_value(['dependencies'], deps + ['boguspackage'])
        project.project_file.save()
        environ = minimal_environ(PROJECT_DIR=dirname)
        result = prepare(project, environ=environ)
        assert not result

    with_directory_contents(
        {DEFAULT_PROJECT_FILENAME: """
dependencies:
    - ipython
    - numpy
"""}, prepare_project_scoped_env_with_packages)


def _run_browser_ui_test(monkeypatch, directory_contents, initial_environ, http_actions, final_result_check):
    from tornado.ioloop import IOLoop
    io_loop = IOLoop()

    def mock_conda_create(prefix, pkgs, channels):
        from anaconda_project.internal.makedirs import makedirs_ok_if_exists
        metadir = os.path.join(prefix, "conda-meta")
        makedirs_ok_if_exists(metadir)
        for p in pkgs:
            pkgmeta = os.path.join(metadir, "%s-0.1.json" % p)
            open(pkgmeta, 'a').close()

    monkeypatch.setattr('anaconda_project.internal.conda_api.create', mock_conda_create)

    http_done = dict()

    def mock_open_new_tab(url):
        @gen.coroutine
        def do_http():
            try:
                for action in http_actions:
                    yield action(url)
            except Exception as e:
                http_done['exception'] = e

            http_done['done'] = True

            io_loop.stop()

        io_loop.add_callback(do_http)

    monkeypatch.setattr('webbrowser.open_new_tab', mock_open_new_tab)

    def do_browser_ui_test(dirname):
        project = Project(dirname)
        assert [] == project.problems
        if not isinstance(initial_environ, dict):
            environ = initial_environ(dirname)
        else:
            environ = initial_environ
        result = prepare(project,
                         environ=environ,
                         io_loop=io_loop,
                         ui_mode=UI_MODE_BROWSER,
                         keep_going_until_success=True)

        # finish up the last http action if prepare_ui.py stopped the loop before we did
        while 'done' not in http_done:
            io_loop.call_later(0.01, lambda: io_loop.stop())
            io_loop.start()

        if 'exception' in http_done:
            raise http_done['exception']

        final_result_check(dirname, result)

    with_directory_contents(directory_contents, do_browser_ui_test)


def _extract_radio_items(response):
    from anaconda_project.internal.plugin_html import _BEAUTIFUL_SOUP_BACKEND
    from bs4 import BeautifulSoup

    if response.code != 200:
        raise Exception("got a bad http response " + repr(response))

    soup = BeautifulSoup(response.body, _BEAUTIFUL_SOUP_BACKEND)
    radios = soup.find_all("input", attrs={'type': 'radio'})
    return radios


def _form_names(response):
    from anaconda_project.internal.plugin_html import _BEAUTIFUL_SOUP_BACKEND
    from bs4 import BeautifulSoup

    if response.code != 200:
        raise Exception("got a bad http response " + repr(response))

    soup = BeautifulSoup(response.body, _BEAUTIFUL_SOUP_BACKEND)
    named_elements = soup.find_all(attrs={'name': True})
    names = set()
    for element in named_elements:
        names.add(element['name'])
    return names


def _prefix_form(form_names, form):
    prefixed = dict()
    for (key, value) in form.items():
        found = False
        for name in form_names:
            if name.endswith("." + key):
                prefixed[name] = value
                found = True
                break
        if not found:
            raise RuntimeError("Form field %s in %r could not be prefixed from %r" % (name, form, form_names))
    return prefixed


def _verify_choices(response, expected):
    name = None
    radios = _extract_radio_items(response)
    actual = []
    for r in radios:
        actual.append((r['value'], 'checked' in r.attrs))
    assert expected == tuple(actual)
    return name


def test_browser_ui_with_default_env_and_no_env_var_set(monkeypatch):
    directory_contents = {DEFAULT_PROJECT_FILENAME: ""}
    initial_environ = minimal_environ_no_conda_env()

    @gen.coroutine
    def get_initial(url):
        response = yield http_get_async(url)
        assert response.code == 200
        body = response.body.decode('utf-8')
        # print("BODY: " + body.encode("ascii", 'ignore').decode('ascii'))
        assert "default' doesn't look like it contains a Conda environment yet." in body
        _verify_choices(response,
                        (
                            # by default, use one of the project-defined named envs
                            ('project', True),
                            # allow typing in a manual value
                            ('variables', False)))

    @gen.coroutine
    def post_empty_form(url):
        response = yield http_post_async(url, body='')
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "Done!" in body
        assert "Using Conda environment" in body
        assert "default" in body
        _verify_choices(response, ())

    def final_result_check(dirname, result):
        assert result
        expected_env_path = os.path.join(dirname, 'envs', 'default')
        expected = dict(CONDA_ENV_PATH=expected_env_path, CONDA_DEFAULT_ENV=expected_env_path, PROJECT_DIR=dirname)
        if platform.system() == 'Windows':
            del expected['CONDA_ENV_PATH']
        assert expected == strip_environ_keeping_conda_env(result.environ)
        bindir = os.path.join(expected_env_path, script_dir)
        assert bindir in result.environ.get("PATH")

    _run_browser_ui_test(monkeypatch=monkeypatch,
                         directory_contents=directory_contents,
                         initial_environ=initial_environ,
                         http_actions=[get_initial, post_empty_form],
                         final_result_check=final_result_check)


def test_browser_ui_with_default_env_and_env_var_set(monkeypatch):
    directory_contents = {DEFAULT_PROJECT_FILENAME: ""}
    envprefix = os.path.join("not", "a", "real", "environment")
    initial_environ = minimal_environ(**{conda_env_var: envprefix})

    stuff = dict()

    @gen.coroutine
    def get_initial(url):
        response = yield http_get_async(url)
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "default' doesn't look like it contains a Conda environment yet." in body
        stuff['form_names'] = _form_names(response)
        _verify_choices(response,
                        (
                            # by default, use one of the project-defined named envs
                            ('project', True),
                            # offer choice to keep the environment setting
                            ('environ', False),
                            # allow typing in a manual value
                            ('variables', False)))

    @gen.coroutine
    def post_choosing_default(url):
        form = _prefix_form(stuff['form_names'], {'source': 'project', 'env_name': 'default'})
        response = yield http_post_async(url, form=form)
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "Done!" in body
        assert "Using Conda environment" in body
        assert "default" in body
        _verify_choices(response, ())

    def final_result_check(dirname, result):
        assert result
        expected_env_path = os.path.join(dirname, 'envs', 'default')
        expected = dict(CONDA_ENV_PATH=expected_env_path, CONDA_DEFAULT_ENV=expected_env_path, PROJECT_DIR=dirname)
        if platform.system() == 'Windows':
            del expected['CONDA_ENV_PATH']
        assert expected == strip_environ_keeping_conda_env(result.environ)
        bindir = os.path.join(expected_env_path, script_dir)
        assert bindir in result.environ.get("PATH")

    _run_browser_ui_test(monkeypatch=monkeypatch,
                         directory_contents=directory_contents,
                         initial_environ=initial_environ,
                         http_actions=[get_initial, post_choosing_default],
                         final_result_check=final_result_check)


def test_browser_ui_with_default_env_and_env_var_set_to_default_already(monkeypatch):
    directory_contents = {DEFAULT_PROJECT_FILENAME: ""}

    def initial_environ(dirname):
        default_env_path = os.path.join(dirname, "envs", "default")
        return minimal_environ(**{conda_env_var: default_env_path})

    stuff = dict()

    @gen.coroutine
    def get_initial(url):
        response = yield http_get_async(url)
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "default' doesn't look like it contains a Conda environment yet." in body
        stuff['form_names'] = _form_names(response)
        _verify_choices(response,
                        (
                            # by default, use one of the project-defined named envs
                            ('project', True),
                            # allow typing in a manual value
                            ('variables', False)))

    @gen.coroutine
    def post_choosing_default(url):
        form = _prefix_form(stuff['form_names'], {'source': 'project', 'env_name': 'default'})
        response = yield http_post_async(url, form=form)
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "Done!" in body
        assert "Using Conda environment" in body
        assert "default" in body
        _verify_choices(response, ())

    def final_result_check(dirname, result):
        assert result
        expected_env_path = os.path.join(dirname, 'envs', 'default')
        expected = dict(CONDA_ENV_PATH=expected_env_path, CONDA_DEFAULT_ENV=expected_env_path, PROJECT_DIR=dirname)
        if platform.system() == 'Windows':
            del expected['CONDA_ENV_PATH']
        assert expected == strip_environ_keeping_conda_env(result.environ)
        bindir = os.path.join(expected_env_path, script_dir)
        assert bindir in result.environ.get("PATH")

    _run_browser_ui_test(monkeypatch=monkeypatch,
                         directory_contents=directory_contents,
                         initial_environ=initial_environ,
                         http_actions=[get_initial, post_choosing_default],
                         final_result_check=final_result_check)


def test_browser_ui_keeping_env_var_set(monkeypatch):
    directory_contents = {DEFAULT_PROJECT_FILENAME: ""}
    envprefix = os.path.join("not", "a", "real", "environment")
    initial_environ = minimal_environ(**{conda_env_var: envprefix})

    stuff = dict()

    @gen.coroutine
    def get_initial(url):
        response = yield http_get_async(url)
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "default' doesn't look like it contains a Conda environment yet." in body
        stuff['form_names'] = _form_names(response)
        _verify_choices(response,
                        (
                            # by default, use one of the project-defined named envs
                            ('project', True),
                            # offer choice to keep the environment setting
                            ('environ', False),
                            # allow typing in a manual value
                            ('variables', False)))

    @gen.coroutine
    def post_choosing_keep_environ(url):
        form = _prefix_form(stuff['form_names'], {'source': 'environ', 'env_name': 'default'})
        response = yield http_post_async(url, form=form)
        assert response.code == 200
        body = response.body.decode('utf-8')
        # print("POST BODY: " + body)
        assert "Done!" not in body
        # error message should be about the environ thing we chose
        assert envprefix + "' doesn't look like it contains a Conda environment yet." in body
        _verify_choices(response,
                        (('project', False),
                         # the thing we chose should still be chosen
                         ('environ', True),
                         ('variables', False)))

    def final_result_check(dirname, result):
        assert not result
        assert ['Browser UI main loop was stopped.'] == result.errors

    _run_browser_ui_test(monkeypatch=monkeypatch,
                         directory_contents=directory_contents,
                         initial_environ=initial_environ,
                         # we choose keep environment twice, should be idempotent
                         http_actions=[get_initial, post_choosing_keep_environ, post_choosing_keep_environ],
                         final_result_check=final_result_check)


def test_browser_ui_two_envs_defaulting_to_first(monkeypatch):
    directory_contents = {DEFAULT_PROJECT_FILENAME: """
environments:
  first_env: {}
  second_env:
    dependencies:
      - python
"""}
    initial_environ = minimal_environ_no_conda_env()

    @gen.coroutine
    def get_initial(url):
        response = yield http_get_async(url)
        assert response.code == 200
        body = response.body.decode('utf-8')
        # print("BODY: " + body)
        assert "first_env' doesn't look like it contains a Conda environment yet." in body
        _verify_choices(response,
                        (
                            # by default, use one of the project-defined named envs
                            ('project', True),
                            # allow typing in a manual value
                            ('variables', False)))

    @gen.coroutine
    def post_empty_form(url):
        response = yield http_post_async(url, body='')
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "Done!" in body
        assert "Using Conda environment" in body
        assert "first_env" in body
        _verify_choices(response, ())

    def final_result_check(dirname, result):
        assert result
        expected_env_path = os.path.join(dirname, 'envs', 'first_env')
        expected = dict(CONDA_ENV_PATH=expected_env_path, CONDA_DEFAULT_ENV=expected_env_path, PROJECT_DIR=dirname)
        if platform.system() == 'Windows':
            del expected['CONDA_ENV_PATH']
        assert expected == strip_environ_keeping_conda_env(result.environ)
        bindir = os.path.join(expected_env_path, script_dir)
        assert bindir in result.environ.get("PATH")

    _run_browser_ui_test(monkeypatch=monkeypatch,
                         directory_contents=directory_contents,
                         initial_environ=initial_environ,
                         http_actions=[get_initial, post_empty_form],
                         final_result_check=final_result_check)


def test_browser_ui_two_envs_choosing_second(monkeypatch):
    directory_contents = {DEFAULT_PROJECT_FILENAME: """
environments:
  first_env:
    dependencies:
      - python
  second_env: {}
"""}
    initial_environ = minimal_environ_no_conda_env()

    stuff = dict()

    @gen.coroutine
    def get_initial(url):
        response = yield http_get_async(url)
        assert response.code == 200
        body = response.body.decode('utf-8')
        stuff['form_names'] = _form_names(response)
        # print("BODY: " + body)
        assert "first_env' doesn't look like it contains a Conda environment yet." in body
        _verify_choices(response,
                        (
                            # by default, use one of the project-defined named envs
                            ('project', True),
                            # allow typing in a manual value
                            ('variables', False)))

    @gen.coroutine
    def post_choosing_second(url):
        form = _prefix_form(stuff['form_names'], {'source': 'project', 'env_name': 'second_env'})
        response = yield http_post_async(url, form=form)
        assert response.code == 200
        body = response.body.decode('utf-8')
        assert "Done!" in body
        assert "Using Conda environment" in body
        assert "second_env" in body
        _verify_choices(response, ())

    def final_result_check(dirname, result):
        assert result
        expected_env_path = os.path.join(dirname, 'envs', 'second_env')
        expected = dict(CONDA_ENV_PATH=expected_env_path, CONDA_DEFAULT_ENV=expected_env_path, PROJECT_DIR=dirname)
        if platform.system() == 'Windows':
            del expected['CONDA_ENV_PATH']
        assert expected == strip_environ_keeping_conda_env(result.environ)
        bindir = os.path.join(expected_env_path, script_dir)
        assert bindir in result.environ.get("PATH")

    _run_browser_ui_test(monkeypatch=monkeypatch,
                         directory_contents=directory_contents,
                         initial_environ=initial_environ,
                         http_actions=[get_initial, post_choosing_second],
                         final_result_check=final_result_check)
