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

import html
import logging
import os
import re
import signal
import socket
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
GRIMOIRELAB_PORT = 8000
GRIMOIRELAB_USER = "admin"
GRIMOIRELAB_PASSWORD = "admin"
GRIMOIRELAB_ECOSYSTEM = "npm-training-set"
GRIMOIRELAB_PROJECT = "npm-popular-components"

OPENSEARCH_IMAGE = "opensearchproject/opensearch:3"
OPENSEARCH_USER = "admin"
OPENSEARCH_PASSWORD = "admin"
OPENSEARCH_INDEX = "events"

GRIMOIRELAB_SERVER_TIMEOUT = 120
GRIMOIRELAB_WORKERS_TIMEOUT = 120

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVED_REPOS_FILE = os.path.join(TESTS_DIR, "data", "archived_repos.spdx.xml")

PORT_IN_USE_HELP = (
    "Port 8000, where the tests run the GrimoireLab server, is already in use.\n"
    "Most likely there are processes or containers left over from a previous "
    "run (they can survive when a run is interrupted). Clean them up with:\n"
    "\n"
    '  pkill -f "grimoirelab run"\n'
    '  docker ps --filter "label=org.testcontainers=true" -q | xargs -r docker rm -f\n'
    "\n"
    "Also stop the docker-compose stack if it is running, as its nginx "
    "container publishes port 8000 too:\n"
    "\n"
    "  docker compose down\n"
    "\n"
    "Verify the port is free afterwards with:\n"
    "\n"
    "  ss -ltnp | grep :8000"
)


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
            cls._check_grimoirelab_port_is_free()
            cls._start_redis_container()
            cls._start_database_container()
            cls._start_opensearch_container()
            cls._start_grimoirelab()
            cls._create_grimoirelab_resources()
            cls._preload_repositories()
        except Exception:
            cls._cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        time.sleep(20)
        cls._cleanup()

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

    @staticmethod
    def _port_is_free(port):
        """Check whether nothing is listening on '127.0.0.1:<port>'."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            return sock.connect_ex(("127.0.0.1", port)) != 0

    @classmethod
    def _check_grimoirelab_port_is_free(cls):
        """Ensure the port used by the GrimoireLab server is available.

        When an orphaned 'grimoirelab run server' (or the nginx container
        of the docker-compose stack) is holding the port, the server of
        the tests dies with a bind error and the requests are answered
        by that orphan, whose database containers no longer exist. The
        symptom is 500 errors like "Can't connect to MySQL server".
        """
        if not cls._port_is_free(GRIMOIRELAB_PORT):
            raise RuntimeError(PORT_IN_USE_HELP)

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
        # through plain HTTP without authentication.
        cls.opensearch_container = (
            OpenSearchContainer(image=OPENSEARCH_IMAGE).with_exposed_ports(9200).with_env("DISABLE_SECURITY_PLUGIN", "true")
        )
        cls.opensearch_container.start()
        wait_for_logs(cls.opensearch_container, ".*recovered .* indices into cluster_state.*")

        port = cls.opensearch_container.get_exposed_port(9200)
        # The port is dynamically assigned by testcontainers; tests must
        # use these attributes instead of hardcoded values.
        cls.opensearch_url = f"http://localhost:{port}"
        cls.opensearch_user = OPENSEARCH_USER
        cls.opensearch_password = OPENSEARCH_PASSWORD

    @classmethod
    def _start_grimoirelab_process(cls, name, args):
        """Start a GrimoireLab process, storing its output in a log file."""
        log_file = tempfile.NamedTemporaryFile(delete=False, prefix=f"grimoirelab_{name}_", suffix=".log")
        cls._grimoirelab_logs[name] = log_file
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
            raise RuntimeError(f"Error running 'grimoirelab admin create-user'. stdout: {proc.stdout}. stderr: {proc.stderr}")

        cls._promote_admin_superuser()

        cls.grimoirelab_server = cls._start_grimoirelab_process("server", ["grimoirelab", "run", "server", "--dev"])
        cls._wait_for_grimoirelab_server()

        cls.grimoirelab_eventizers = cls._start_grimoirelab_process(
            "eventizers", ["grimoirelab", "run", "eventizers", "--workers", "10"]
        )
        cls.grimoirelab_archivists = cls._start_grimoirelab_process(
            "archivists", ["grimoirelab", "run", "archivists", "--workers", "10"]
        )
        # docker-compose also runs the ushers, which dispatch the
        # scheduled tasks; run them too.
        cls.grimoirelab_ushers = cls._start_grimoirelab_process("ushers", ["grimoirelab", "run", "ushers"])

        # Fail fast (with logs) if any worker crashes on startup, instead
        # of failing later with an obscure timeout.
        cls._wait_for_worker_logs("eventizers", cls.grimoirelab_eventizers)
        cls._wait_for_worker_logs("archivists", cls.grimoirelab_archivists)

        time.sleep(3)
        if cls.grimoirelab_ushers.poll() is not None:
            logging.warning(
                "'grimoirelab run ushers' exited unexpectedly; continuing without it.\n" + cls._dump_grimoirelab_logs()
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
            raise RuntimeError(f"Error promoting '{GRIMOIRELAB_USER}' to superuser. stdout: {proc.stdout}. stderr: {proc.stderr}")

    @classmethod
    def _wait_for_grimoirelab_server(cls, timeout=GRIMOIRELAB_SERVER_TIMEOUT):
        """Wait until the GrimoireLab server accepts requests."""
        start = time.time()
        while time.time() - start < timeout:
            if cls.grimoirelab_server.poll() is not None:
                raise RuntimeError("GrimoireLab server exited unexpectedly.\n" + cls._dump_grimoirelab_logs())
            try:
                urllib.request.urlopen(GRIMOIRELAB_URL, timeout=5)
                cls._confirm_server_is_ours()
                return
            except urllib.error.HTTPError:
                cls._confirm_server_is_ours()
                return
            except RuntimeError:
                raise
            except Exception:
                time.sleep(1)

        raise RuntimeError(f"GrimoireLab server was not ready after {timeout} seconds.\n" + cls._dump_grimoirelab_logs())

    @classmethod
    def _confirm_server_is_ours(cls):
        """Confirm the server answering the requests is the one the tests started.

        If port 8000 is taken by another process (an orphaned
        'grimoirelab run server' from a previous run, or the nginx
        container of the docker-compose stack), the server of the tests
        dies with a bind error while that other process answers the
        requests using the ports of containers that no longer exist.
        """
        time.sleep(2)
        if cls.grimoirelab_server.poll() is not None:
            raise RuntimeError(
                "The GrimoireLab server process of the tests died right after "
                "starting, but SOMETHING is still answering on port 8000. "
                "Another process took the port and is answering the requests "
                "with the configuration of a previous run.\n\n"
                + PORT_IN_USE_HELP
                + "\n\n--- server output ---\n"
                + cls._dump_grimoirelab_logs()
            )

    @classmethod
    def _wait_for_worker_logs(cls, name, proc, timeout=GRIMOIRELAB_WORKERS_TIMEOUT):
        """Wait until a worker process reports it is listening for jobs."""
        start = time.time()
        log_path = cls._grimoirelab_logs[name].name

        while time.time() - start < timeout:
            if proc.poll() is not None:
                raise RuntimeError(f"GrimoireLab '{name}' process exited unexpectedly.\n" + cls._dump_grimoirelab_logs())
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

    @classmethod
    def _create_grimoirelab_resources(cls):
        """Create the ecosystem and the project used by the tests.

        The metrics CLI takes 'grimoirelab-ecosystem' and
        'grimoirelab-project' as parameters and queries their repos
        endpoint, but it does not create them; they must exist
        beforehand, or every call becomes a 404.
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
        try:
            response = requests.post(
                f"{GRIMOIRELAB_URL}/token/",
                json={"username": GRIMOIRELAB_USER, "password": GRIMOIRELAB_PASSWORD},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Error connecting to the GrimoireLab server: {exc!r}\n\n" + cls._server_diagnostics())

        if response.status_code != 200:
            raise RuntimeError(
                "Error getting the API token. "
                f"Status: {response.status_code}. "
                f"Detail: {cls._html_error_detail(response.text)}\n\n" + cls._server_diagnostics()
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
    def _html_error_detail(body):
        """Extract the exception message from a Django debug page."""
        match = re.search(r'<pre class="exception_value">(.*?)</pre>', body, re.DOTALL)
        if match:
            return html.unescape(match.group(1)).strip()
        # Not a Django debug page; return a truncated copy.
        return body[:500]

    @classmethod
    def _server_diagnostics(cls):
        """Diagnostics to append when a request to the server fails."""
        lines = []

        server = getattr(cls, "grimoirelab_server", None)
        if server is not None:
            if server.poll() is None:
                lines.append("* Server process started by the tests: running.")
            else:
                lines.append(
                    f"* Server process started by the tests: DEAD (exit code {server.returncode}). "
                    "The requests were answered by ANOTHER process using port 8000."
                )

        db_port = os.environ.get("GRIMOIRELAB_DB_PORT")
        if db_port:
            if cls._port_is_free(int(db_port)):
                lines.append(
                    f"* MariaDB container on port {db_port}: NOT reachable. Either the "
                    "container died, or the requests were answered by an orphaned server "
                    "configured with the ports of a previous run."
                )
            else:
                lines.append(f"* MariaDB container on port {db_port}: reachable.")

        lines.append(cls._dump_grimoirelab_logs())
        return "\n".join(lines)

    @classmethod
    def _api_get_or_create(cls, headers, get_url, post_url, body, kind):
        """Create a resource through the API if it does not exist yet."""
        response = requests.get(get_url, headers=headers, timeout=30)
        if response.status_code == 200:
            return

        if response.status_code != 404:
            raise RuntimeError(
                f"Error checking the {kind}. Status: {response.status_code}. "
                f"Detail: {cls._html_error_detail(response.text)}\n\n" + cls._server_diagnostics()
            )

        response = requests.post(post_url, headers=headers, json=body, timeout=30)
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Error creating the {kind}. Status: {response.status_code}. "
                f"Detail: {cls._html_error_detail(response.text)}\n\n" + cls._server_diagnostics()
            )

    @classmethod
    def _preload_repositories(cls):
        cls._reset_logging()
        result = cls.runner.invoke(
            grimoirelab_metrics,
            [
                ARCHIVED_REPOS_FILE,
                "--grimoirelab-url",
                GRIMOIRELAB_URL,
                "--grimoirelab-user",
                GRIMOIRELAB_USER,
                "--grimoirelab-password",
                GRIMOIRELAB_PASSWORD,
                "--grimoirelab-ecosystem",
                GRIMOIRELAB_ECOSYSTEM,
                "--grimoirelab-project",
                GRIMOIRELAB_PROJECT,
                "--opensearch-url",
                cls.opensearch_url,
                "--opensearch-user",
                cls.opensearch_user,
                "--opensearch-password",
                cls.opensearch_password,
                "--opensearch-index",
                OPENSEARCH_INDEX,
                "--output",
                cls.temp_file.name,
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
