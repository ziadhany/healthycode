#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from unittest import TestCase

import requests
from click.testing import CliRunner
from testcontainers.redis import RedisContainer
from testcontainers.mysql import MySqlContainer
from testcontainers.opensearch import OpenSearchContainer
from testcontainers.core.waiting_utils import wait_for_logs

from grimoirelab_metrics.cli import grimoirelab_metrics

GRIMOIRELAB_URL = "http://localhost:8000"
GRIMOIRELAB_USER = "admin"
GRIMOIRELAB_PASSWORD = "admin"
GRIMOIRELAB_ECOSYSTEM = "npm-training-set"
GRIMOIRELAB_PROJECT = "npm-popular-components"

# The default OpenSearch image used by testcontainers (1.3.x) is too old
# for GrimoireLab: it rejects the 'copy_alias' field of the ISM rollover
# action, which makes the archivists crash on startup. A recent image is
# required (docker-compose uses 'opensearchproject/opensearch:3').
OPENSEARCH_IMAGE = "opensearchproject/opensearch:2.19.2"
OPENSEARCH_USER = "admin"
OPENSEARCH_PASSWORD = "admin"
OPENSEARCH_INDEX = "events"

GRIMOIRELAB_SERVER_TIMEOUT = 120
GRIMOIRELAB_WORKERS_TIMEOUT = 120


class EndToEndTestCase(TestCase):
    """Base class to build end to end tests.

    This class contains all necessary to build end to end test cases
    for GrimoireLab metrics. It provides an OpenSearch server and a
    GrimoireLab 2.x server and workers, along with its required
    MariaDB and Redis databases.

    It also creates the ecosystem and the project that the metrics CLI
    expects to find in the server (the CLI uses them but does not
    create them).

    Containers are started on random ports. Therefore, tests must use
    the attributes `opensearch_url`, `opensearch_user` and
    `opensearch_password` to connect to the OpenSearch server, instead
    of hardcoding connection parameters.
    """

    @classmethod
    def setUpClass(cls):
        cls.temp_file = tempfile.NamedTemporaryFile(delete=False)
        cls.runner = CliRunner()
        cls._grimoirelab_logs = {}

        try:
            cls._start_redis_container()
            cls._start_database_container()
            cls._start_opensearch_container()
            cls._start_grimoirelab()
            cls._create_grimoirelab_resources()
            cls._preload_repositories()
        except Exception:
            # unittest does not call tearDownClass when setUpClass fails,
            # so clean up here to avoid leaking containers and processes.
            cls._cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        # Give the workers some extra time to finish their pending jobs.
        time.sleep(20)
        cls._cleanup()

    ##
    # Command execution helpers
    ##

    @staticmethod
    def _reset_logging():
        """Remove the logging handlers configured by previous invocations.

        CliRunner swaps 'sys.stdout' and 'sys.stderr' on every invocation.
        Any logging handler configured while a previous invocation was
        running keeps writing to that old invocation's buffer. Without
        this reset, the logs and error messages of the next invocation
        are written to a buffer nobody reads, and 'result.output' comes
        out empty.
        """
        root = logging.getLogger()
        root.handlers = []
        root.setLevel(logging.INFO)

    @staticmethod
    def _result_details(result):
        """Human readable details of a CliRunner result, including stderr."""
        try:
            stderr = result.stderr
        except (AttributeError, ValueError):
            stderr = "<stderr not captured>"

        details = (
            f"exit code: {result.exit_code}; "
            f"stdout: {result.output!r}; "
            f"stderr: {stderr!r}; "
            f"exception: {result.exception!r}"
        )

        if result.exception is not None and not isinstance(result.exception, SystemExit):
            details += "\n" + "".join(traceback.format_exception(result.exception))

        return details

    ##
    # Containers
    ##

    @classmethod
    def _start_database_container(cls):
        cls.mysql_container = MySqlContainer(image="mariadb:latest", root_password="root").with_exposed_ports(3306)
        cls.mysql_container.start()

    @classmethod
    def _start_redis_container(cls):
        cls.redis_container = RedisContainer().with_exposed_ports(6379)
        cls.redis_container.start()

    @classmethod
    def _start_opensearch_container(cls):
        # Keep the security plugin disabled so the server is reachable
        # through plain HTTP without authentication, like before.
        cls.opensearch_container = (
            OpenSearchContainer(image=OPENSEARCH_IMAGE)
            .with_exposed_ports(9200)
            .with_env("DISABLE_SECURITY_PLUGIN", "true")
        )
        cls.opensearch_container.start()
        wait_for_logs(cls.opensearch_container, ".*recovered .* indices into cluster_state.*")

        port = cls.opensearch_container.get_exposed_port(9200)
        # The port is dynamically assigned by testcontainers; tests must
        # use these attributes instead of hardcoded values.
        cls.opensearch_url = f"http://localhost:{port}"
        cls.opensearch_user = OPENSEARCH_USER
        cls.opensearch_password = OPENSEARCH_PASSWORD

    ##
    # GrimoireLab
    ##

    @classmethod
    def _start_grimoirelab_process(cls, name, args):
        """Start a GrimoireLab process, storing its output in a log file."""
        log_file = tempfile.NamedTemporaryFile(delete=False, prefix=f"grimoirelab_{name}_", suffix=".log")
        cls._grimoirelab_logs[name] = log_file

        # Unbuffered output, so logs reach the file while the process
        # is still running (needed by '_wait_for_worker_logs').
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"

        return subprocess.Popen(
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    @classmethod
    def _dump_grimoirelab_logs(cls):
        """Return the output produced by the GrimoireLab processes."""
        sections = []

        for name, log_file in cls._grimoirelab_logs.items():
            try:
                log_file.flush()
                with open(log_file.name, errors="replace") as f:
                    content = f.read().strip() or "<empty>"
            except OSError:
                content = "<could not be read>"
            sections.append(f"--- {name} ---\n{content}")

        return "\n".join(sections) if sections else "<no GrimoireLab processes were started>"

    @classmethod
    def _start_grimoirelab(cls):
        env = os.environ
        env["DJANGO_SETTINGS_MODULE"] = "grimoirelab.core.config.settings"
        env["GRIMOIRELAB_REDIS_PORT"] = str(cls.redis_container.get_exposed_port(6379))
        env["GRIMOIRELAB_DB_PORT"] = str(cls.mysql_container.get_exposed_port(3306))
        env["GRIMOIRELAB_DB_PASSWORD"] = cls.mysql_container.root_password
        env["GRIMOIRELAB_ARCHIVIST_STORAGE_URL"] = (
            f"http://{cls.opensearch_user}:{cls.opensearch_password}"
            f"@localhost:{cls.opensearch_container.get_exposed_port(9200)}"
        )
        # Make sure the archivists store the events in the same index
        # the CLI reads them from.
        env["GRIMOIRELAB_ARCHIVIST_STORAGE_INDEX"] = OPENSEARCH_INDEX
        env["GRIMOIRELAB_USER_PASSWORD"] = GRIMOIRELAB_PASSWORD
        env["GRIMOIRELAB_ARCHIVIST_BLOCK_TIMEOUT"] = "1000"

        from grimoirelab.core.runner.cmd import grimoirelab

        # 'admin setup' ends with an interactive prompt asking whether a
        # superuser should be created. Without an answer (EOF), click aborts
        # the command with exit code 1. Answer "no": the privileged 'admin'
        # user needed by the tests is created right after this.
        cls._reset_logging()
        result = cls.runner.invoke(grimoirelab, ["admin", "setup"], input="n\n")
        if result.exit_code != 0:
            raise RuntimeError("Error running 'grimoirelab admin setup'. " + cls._result_details(result))

        proc = subprocess.run(
            ["grimoirelab", "admin", "create-user", "--username", GRIMOIRELAB_USER, "--no-interactive"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Error running 'grimoirelab admin create-user'. stdout: {proc.stdout}. stderr: {proc.stderr}"
            )

        cls._promote_admin_superuser()

        cls.grimoirelab_server = cls._start_grimoirelab_process(
            "server", ["grimoirelab", "run", "server", "--dev"]
        )
        cls._wait_for_grimoirelab_server()

        cls.grimoirelab_eventizers = cls._start_grimoirelab_process(
            "eventizers", ["grimoirelab", "run", "eventizers", "--workers", "10"]
        )
        cls.grimoirelab_archivists = cls._start_grimoirelab_process(
            "archivists", ["grimoirelab", "run", "archivists", "--workers", "10"]
        )
        # docker-compose also runs the ushers, which dispatch the
        # scheduled tasks; run them too.
        cls.grimoirelab_ushers = cls._start_grimoirelab_process(
            "ushers", ["grimoirelab", "run", "ushers"]
        )

        # Fail fast (with logs) if any worker crashes on startup, instead
        # of failing later with an obscure timeout.
        cls._wait_for_worker_logs("eventizers", cls.grimoirelab_eventizers)
        cls._wait_for_worker_logs("archivists", cls.grimoirelab_archivists)

        time.sleep(3)
        if cls.grimoirelab_ushers.poll() is not None:
            logging.warning(
                "'grimoirelab run ushers' exited unexpectedly; continuing without it.\n"
                + cls._dump_grimoirelab_logs()
            )

    @staticmethod
    def _promote_admin_superuser():
        """Give the test user superuser privileges.

        The tests use the 'admin' user to create ecosystems, projects and
        repositories through the API, which requires superuser privileges.
        """
        code = (
            "import django; django.setup(); "
            "from django.contrib.auth import get_user_model; "
            "User = get_user_model(); "
            f"User.objects.filter(username='{GRIMOIRELAB_USER}').update(is_superuser=True, is_staff=True)"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Error promoting '{GRIMOIRELAB_USER}' to superuser. stdout: {proc.stdout}. stderr: {proc.stderr}"
            )

    @classmethod
    def _wait_for_grimoirelab_server(cls, timeout=GRIMOIRELAB_SERVER_TIMEOUT):
        """Wait until the GrimoireLab server accepts requests."""
        start = time.time()
        while time.time() - start < timeout:
            if cls.grimoirelab_server.poll() is not None:
                raise RuntimeError("GrimoireLab server exited unexpectedly.\n" + cls._dump_grimoirelab_logs())
            try:
                urllib.request.urlopen(GRIMOIRELAB_URL, timeout=5)
                return
            except urllib.error.HTTPError:
                # Any HTTP response means the server is already running.
                return
            except Exception:
                time.sleep(1)

        raise RuntimeError(
            f"GrimoireLab server was not ready after {timeout} seconds.\n" + cls._dump_grimoirelab_logs()
        )

    @classmethod
    def _wait_for_worker_logs(cls, name, proc, timeout=GRIMOIRELAB_WORKERS_TIMEOUT):
        """Wait until a worker process reports it is listening for jobs."""
        start = time.time()
        log_path = cls._grimoirelab_logs[name].name

        while time.time() - start < timeout:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"GrimoireLab '{name}' process exited unexpectedly.\n" + cls._dump_grimoirelab_logs()
                )
            try:
                with open(log_path, errors="replace") as f:
                    if "*** Listening on" in f.read():
                        return
            except OSError:
                pass
            time.sleep(1)

        logging.warning(
            "GrimoireLab '%s' did not report listening for jobs after %s seconds; continuing anyway.",
            name,
            timeout,
        )

    ##
    # Ecosystem and project creation
    ##

    @classmethod
    def _create_grimoirelab_resources(cls):
        """Create the ecosystem and the project used by the tests.

        The metrics CLI takes 'grimoirelab-ecosystem' and
        'grimoirelab-project' as parameters and queries their repos
        endpoint, but it does not create them; they must exist
        beforehand, or every call becomes a 404 on
        'ecosystems/None/projects/None'.
        """
        api_url = f"{GRIMOIRELAB_URL}/api/v1"
        headers = cls._api_headers(api_url)

        cls._api_get_or_create(
            headers=headers,
            get_url=f"{api_url}/ecosystems/{GRIMOIRELAB_ECOSYSTEM}/",
            post_url=f"{api_url}/ecosystems/",
            body={"name": GRIMOIRELAB_ECOSYSTEM},
            kind="ecosystem",
        )
        cls._api_get_or_create(
            headers=headers,
            get_url=f"{api_url}/ecosystems/{GRIMOIRELAB_ECOSYSTEM}/projects/{GRIMOIRELAB_PROJECT}/",
            post_url=f"{api_url}/ecosystems/{GRIMOIRELAB_ECOSYSTEM}/projects/",
            body={"name": GRIMOIRELAB_PROJECT},
            kind="project",
        )

    @classmethod
    def _api_headers(cls, api_url):
        """Return the headers to authenticate against the GrimoireLab API."""
        response = requests.post(
            f"{GRIMOIRELAB_URL}/token/",
            json={"username": GRIMOIRELAB_USER, "password": GRIMOIRELAB_PASSWORD},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Error getting the API token. Status: {response.status_code}. Body: {response.text}"
            )

        data = response.json()
        token = data.get("token") or data.get("access")
        if not token:
            raise RuntimeError(f"Unexpected '/token/' response: {data}")

        # Try the usual authentication schemes until one is accepted.
        for scheme in ("Bearer", "Token", "JWT"):
            headers = {"Authorization": f"{scheme} {token}"}
            check = requests.get(f"{api_url}/ecosystems/", headers=headers, timeout=30)
            if check.status_code not in (401, 403):
                return headers

        raise RuntimeError("None of the authentication schemes was accepted by the GrimoireLab API.")

    @staticmethod
    def _api_get_or_create(headers, get_url, post_url, body, kind):
        """Create a resource through the API if it does not exist yet."""
        response = requests.get(get_url, headers=headers, timeout=30)
        if response.status_code == 200:
            return

        if response.status_code != 404:
            raise RuntimeError(
                f"Error checking the {kind}. Status: {response.status_code}. Body: {response.text}"
            )

        response = requests.post(post_url, headers=headers, json=body, timeout=30)
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Error creating the {kind}. Status: {response.status_code}. Body: {response.text}"
            )

    ##
    # Preload
    ##

    @classmethod
    def _preload_repositories(cls):
        cls._reset_logging()
        result = cls.runner.invoke(
            grimoirelab_metrics,
            [
                "./data/archived_repos.spdx.xml",
                "--grimoirelab-url", GRIMOIRELAB_URL,
                "--grimoirelab-user", GRIMOIRELAB_USER,
                "--grimoirelab-password", GRIMOIRELAB_PASSWORD,
                "--grimoirelab-ecosystem", GRIMOIRELAB_ECOSYSTEM,
                "--grimoirelab-project", GRIMOIRELAB_PROJECT,
                "--opensearch-url", cls.opensearch_url,
                "--opensearch-user", cls.opensearch_user,
                "--opensearch-password", cls.opensearch_password,
                "--opensearch-index", OPENSEARCH_INDEX,
                "--output", cls.temp_file.name,
                "--from-date=2000-01-01",
            ],
        )
        if result.exit_code != 0:
            raise RuntimeError(
                "Error preloading the repositories. "
                + cls._result_details(result)
                + "\n--- GrimoireLab processes output ---\n"
                + cls._dump_grimoirelab_logs()
            )
        time.sleep(20)

    ##
    # Teardown
    ##

    @classmethod
    def _cleanup(cls):
        """Stop GrimoireLab processes and containers, and remove temp files.

        It is safe to call it on partially initialized classes.
        """
        cls._stop_process_group(getattr(cls, "grimoirelab_eventizers", None))
        cls._stop_process_group(getattr(cls, "grimoirelab_archivists", None))
        cls._stop_process_group(getattr(cls, "grimoirelab_ushers", None))
        cls._stop_process_group(getattr(cls, "grimoirelab_server", None))

        for name in ("redis_container", "mysql_container", "opensearch_container"):
            container = getattr(cls, name, None)
            if container is not None:
                try:
                    container.stop()
                except Exception:
                    pass

        for log_file in getattr(cls, "_grimoirelab_logs", {}).values():
            try:
                log_file.close()
                os.remove(log_file.name)
            except Exception:
                pass

        temp_file = getattr(cls, "temp_file", None)
        if temp_file is not None:
            try:
                filename = temp_file.name
                temp_file.close()
                if os.path.exists(filename):
                    os.remove(filename)
            except Exception:
                pass

    @staticmethod
    def _stop_process_group(proc):
        """Terminate a process together with its whole process group."""
        if proc is None:
            return

        if proc.poll() is None:
            try:
                pgid = os.getpgid(proc.pid)
                if pgid == proc.pid:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()