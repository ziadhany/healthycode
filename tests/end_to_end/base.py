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

import os
import signal
import subprocess
import sys
import tempfile
import time
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

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVED_REPOS_FILE = os.path.join(TESTS_DIR, "data", "archived_repos.spdx.xml")


class EndToEndTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_file = tempfile.NamedTemporaryFile(delete=False)
        cls.runner = CliRunner()

        try:
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
        cls.opensearch_container = (
            OpenSearchContainer(image=OPENSEARCH_IMAGE).with_exposed_ports(9200).with_env("DISABLE_SECURITY_PLUGIN", "true")
        )
        cls.opensearch_container.start()
        wait_for_logs(cls.opensearch_container, ".*recovered .* indices into cluster_state.*")

        port = cls.opensearch_container.get_exposed_port(9200)
        cls.opensearch_url = f"http://localhost:{port}"
        cls.opensearch_user = OPENSEARCH_USER
        cls.opensearch_password = OPENSEARCH_PASSWORD

    @classmethod
    def _start_grimoirelab_process(cls, args):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        return subprocess.Popen(args, start_new_session=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        env["GRIMOIRELAB_ARCHIVIST_STORAGE_INDEX"] = OPENSEARCH_INDEX
        env["GRIMOIRELAB_USER_PASSWORD"] = GRIMOIRELAB_PASSWORD
        env["GRIMOIRELAB_ARCHIVIST_BLOCK_TIMEOUT"] = "1000"

        from grimoirelab.core.runner.cmd import grimoirelab

        cls.runner.invoke(grimoirelab, ["admin", "setup"], input="n\n")
        subprocess.run(
            ["grimoirelab", "admin", "create-user", "--username", GRIMOIRELAB_USER, "--no-interactive"],
            capture_output=True,
        )

        cls._promote_admin_superuser()

        cls.grimoirelab_server = cls._start_grimoirelab_process(["grimoirelab", "run", "server", "--dev"])
        cls._wait_for_grimoirelab_server()

        cls.grimoirelab_eventizers = cls._start_grimoirelab_process(["grimoirelab", "run", "eventizers", "--workers", "10"])
        cls.grimoirelab_archivists = cls._start_grimoirelab_process(["grimoirelab", "run", "archivists", "--workers", "10"])
        cls.grimoirelab_ushers = cls._start_grimoirelab_process(["grimoirelab", "run", "ushers"])
        time.sleep(5)

    @staticmethod
    def _promote_admin_superuser():
        code = (
            "import django; django.setup(); "
            "from django.contrib.auth import get_user_model; "
            "User = get_user_model(); "
            f"User.objects.filter(username='{GRIMOIRELAB_USER}').update(is_superuser=True, is_staff=True)"
        )
        subprocess.run([sys.executable, "-c", code], capture_output=True)

    @classmethod
    def _wait_for_grimoirelab_server(cls, timeout=120):
        start = time.time()
        while time.time() - start < timeout:
            try:
                urllib.request.urlopen(GRIMOIRELAB_URL, timeout=5)
                return
            except Exception:
                time.sleep(1)
        raise RuntimeError("GrimoireLab server failed to start.")

    @classmethod
    def _create_grimoirelab_resources(cls):
        api_url = f"{GRIMOIRELAB_URL}/api/v1"
        headers = cls._api_headers(api_url)

        cls._api_get_or_create(
            headers=headers,
            get_url=f"{api_url}/ecosystems/{GRIMOIRELAB_ECOSYSTEM}/",
            post_url=f"{api_url}/ecosystems/",
            body={"name": GRIMOIRELAB_ECOSYSTEM},
        )
        cls._api_get_or_create(
            headers=headers,
            get_url=f"{api_url}/ecosystems/{GRIMOIRELAB_ECOSYSTEM}/projects/{GRIMOIRELAB_PROJECT}/",
            post_url=f"{api_url}/ecosystems/{GRIMOIRELAB_ECOSYSTEM}/projects/",
            body={"name": GRIMOIRELAB_PROJECT},
        )

    @classmethod
    def _api_headers(cls, api_url):
        response = requests.post(
            f"{GRIMOIRELAB_URL}/token/",
            json={"username": GRIMOIRELAB_USER, "password": GRIMOIRELAB_PASSWORD},
            timeout=30,
        )
        token = response.json().get("token") or response.json().get("access")

        for scheme in ("Bearer", "Token", "JWT"):
            headers = {"Authorization": f"{scheme} {token}"}
            if requests.get(f"{api_url}/ecosystems/", headers=headers, timeout=30).status_code not in (401, 403):
                return headers

    @classmethod
    def _api_get_or_create(cls, headers, get_url, post_url, body):
        if requests.get(get_url, headers=headers, timeout=30).status_code != 200:
            requests.post(post_url, headers=headers, json=body, timeout=30)

    @classmethod
    def _preload_repositories(cls):
        cls.runner.invoke(
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
        time.sleep(20)

    @classmethod
    def _cleanup(cls):
        cls._stop_process_group(getattr(cls, "grimoirelab_eventizers", None))
        cls._stop_process_group(getattr(cls, "grimoirelab_archivists", None))
        cls._stop_process_group(getattr(cls, "grimoirelab_ushers", None))
        cls._stop_process_group(getattr(cls, "grimoirelab_server", None))

        for name in ("redis_container", "mysql_container", "opensearch_container"):
            container = getattr(cls, name, None)
            if container:
                try:
                    container.stop()
                except Exception:
                    pass

        temp_file = getattr(cls, "temp_file", None)
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.remove(temp_file.name)
            except Exception:
                pass

    @staticmethod
    def _stop_process_group(proc):
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
