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

import datetime
import json
import logging
import os
import tempfile
import unittest
import httpretty
from unittest.mock import patch

from click.testing import CliRunner
from grimoirelab_metrics.cli import grimoirelab_metrics, get_repository


GRIMOIRELAB_URL = "http://localhost:8000"
GRIMOIRELAB_USER = "admin"
GRIMOIRELAB_PASSWORD = "admin"
GRIMOIRELAB_ECOSYSTEM = "npm-training-set"
GRIMOIRELAB_PROJECT = "npm-popular-components"

OPENSEARCH_URL = "https://localhost:9200"
OPENSEARCH_USER = "admin"
OPENSEARCH_PASSWORD = "admin"
OPENSEARCH_INDEX = "events"

ERROR_GRIMOIRELAB_URL = "http://localhost:8001"

REPOSITORIES_URL = f"{GRIMOIRELAB_URL}/api/v1/ecosystems/{GRIMOIRELAB_ECOSYSTEM}" f"/projects/{GRIMOIRELAB_PROJECT}/repos/"
ERROR_REPOSITORIES_URL = (
    f"{ERROR_GRIMOIRELAB_URL}/api/v1/ecosystems/{GRIMOIRELAB_ECOSYSTEM}" f"/projects/{GRIMOIRELAB_PROJECT}/repos/"
)


def command_args(source, output_path, *extra, grimoirelab_url=GRIMOIRELAB_URL):
    """Build the invocation exactly like collect_and_store_grimoire_metric()"""
    return [
        source,
        "--grimoirelab-url",
        grimoirelab_url,
        "--grimoirelab-user",
        GRIMOIRELAB_USER,
        "--grimoirelab-password",
        GRIMOIRELAB_PASSWORD,
        "--grimoirelab-ecosystem",
        GRIMOIRELAB_ECOSYSTEM,
        "--grimoirelab-project",
        GRIMOIRELAB_PROJECT,
        "--opensearch-url",
        OPENSEARCH_URL,
        "--opensearch-index",
        OPENSEARCH_INDEX,
        "--opensearch-user",
        OPENSEARCH_USER,
        "--opensearch-password",
        OPENSEARCH_PASSWORD,
        "--output",
        output_path,
        *extra,
    ]


def register_auth_endpoint(base_url):
    """Mock authenticate endpoint"""

    def token_callback(request, uri, headers):
        body = json.dumps({"token": "fake-token", "access": "fake-token", "access_token": "fake-token"})
        return (200, headers, body)

    httpretty.register_uri(
        httpretty.POST,
        f"{base_url}/token/",
        responses=[httpretty.Response(body=token_callback)],
    )


def repository_data(uri, status, last_run):
    """Build the repositories payload returned by endpoint"""

    return {
        "count": 1,
        "results": [
            {
                "uri": uri,
                "categories": [{"task": {"last_run": last_run, "status": status}}],
            }
        ],
    }


def setup_grimoirelab_mock_server(never_ending=False):
    """Set up a GrimoireLab API mock used by the CLI."""
    register_auth_endpoint(GRIMOIRELAB_URL)

    post_requests = []
    get_requests = []
    scheduled = set()

    def get_callback(request, uri, headers):
        get_requests.append(request)
        repo_uri = request.querystring.get("uri", [None])[0]

        if repo_uri not in scheduled:
            data = {"count": 0, "results": []}
        elif never_ending:
            last_run = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)
            data = repository_data(repo_uri, "running", last_run.isoformat())
        else:
            last_run = datetime.datetime.now(datetime.timezone.utc)
            data = repository_data(repo_uri, "completed", last_run.isoformat())

        return (200, headers, json.dumps(data))

    def post_callback(request, uri, headers):
        try:
            repo_uri = json.loads(request.body)["uri"]
        except (ValueError, KeyError):
            repo_uri = None

        if repo_uri and repo_uri not in scheduled:
            scheduled.add(repo_uri)
            post_requests.append(request)

        return (200, headers, json.dumps({"message": "Task scheduled correctly"}))

    httpretty.register_uri(
        httpretty.GET,
        REPOSITORIES_URL,
        responses=[httpretty.Response(body=get_callback)],
    )
    httpretty.register_uri(
        httpretty.POST,
        REPOSITORIES_URL,
        responses=[httpretty.Response(body=post_callback)],
    )

    return post_requests, get_requests


def setup_grimoirelab_error_mock_server():
    """Set up a mock server whose repositories API always returns 500"""

    register_auth_endpoint(ERROR_GRIMOIRELAB_URL)

    post_requests = []

    def error_callback(request, uri, headers):
        return (500, headers, json.dumps({"detail": "internal server error"}))

    httpretty.register_uri(
        httpretty.GET,
        ERROR_REPOSITORIES_URL,
        responses=[httpretty.Response(body=error_callback)],
    )
    httpretty.register_uri(
        httpretty.POST,
        ERROR_REPOSITORIES_URL,
        responses=[httpretty.Response(body=error_callback)],
    )

    return post_requests


class TestCli(unittest.TestCase):
    def setUp(self):
        logging.getLogger().handlers = []
        # temporary file for output metrics
        self.temp_file = tempfile.NamedTemporaryFile(delete=False)

    def tearDown(self):
        os.remove(self.temp_file.name)

    @httpretty.activate
    @patch("grimoirelab_metrics.cli.get_repository_metrics")
    def test_valid_file(self, mock_get_repository_metrics):
        """Check if it schedules tasks to analyze all git repositories from a valid file"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server()
        mock_get_repository_metrics.return_value = {"metrics": {"num_commits": 10}}

        runner = CliRunner()
        result = runner.invoke(grimoirelab_metrics, command_args("./data/valid.spdx.xml", self.temp_file.name))

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Found 5 git repositories", result.output)
        self.assertIn("Scheduling data collection tasks", result.output)
        self.assertNotIn("Scheduling task to fetch commits", result.output)
        self.assertEqual(len(http_requests), 5)
        self.assertEqual(len(http_requests_repos), 10)

        expected_packages = [
            "SPDXRef-bootstrap-gnu-config.bst-0",
            "SPDXRef-public-linux-headers.bst-6.10.2",
            "SPDXRef-bootstrap-glibc.bst-2.40",
            "SPDXRef-bootstrap-attr.bst-2.5.2",
            "SPDXRef-bootstrap-acl.bst-2.3.2",
        ]
        with open(self.temp_file.name) as f:
            metrics = json.load(f)
            self.assertEqual(len(metrics["packages"]), 5)
            i = 0
            for package, data in metrics["packages"].items():
                self.assertEqual(package, expected_packages[i])
                self.assertEqual(data["metrics"]["num_commits"], 10)
                i += 1

    @httpretty.activate
    @patch("grimoirelab_metrics.cli.get_repository_metrics")
    def test_verbose(self, mock_get_repository_metrics):
        """Check if it logs all information when using '--verbose'"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server()
        mock_get_repository_metrics.return_value = {"metrics": {"num_commits": 10}}

        runner = CliRunner()
        result = runner.invoke(grimoirelab_metrics, command_args("./data/valid.spdx.xml", self.temp_file.name, "--verbose"))

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Found 5 git repositories", result.output)
        self.assertIn("Scheduling data collection tasks", result.output)
        self.assertIn("Scheduling task to fetch commits", result.output)
        self.assertEqual(len(http_requests), 5)
        self.assertEqual(len(http_requests_repos), 10)

    @httpretty.activate
    def test_invalid_file_type(self):
        """Check if it returns an error when the file type is not valid"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server()

        runner = CliRunner()
        result = runner.invoke(grimoirelab_metrics, command_args("invalid.doc", self.temp_file.name))

        self.assertEqual(result.exit_code, 0)
        self.assertIn("The source is not a file and does not end with .git", result.output)
        self.assertEqual(len(http_requests), 0)
        self.assertEqual(len(http_requests_repos), 0)

    @httpretty.activate
    def test_invalid_sbom_format(self):
        """Check if it returns an error when the SBoM is not formatted correctly"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server()

        runner = CliRunner()
        result = runner.invoke(grimoirelab_metrics, command_args("./data/invalid_format.spdx.json", self.temp_file.name))

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Error while parsing document", result.output)
        self.assertEqual(len(http_requests), 0)
        self.assertEqual(len(http_requests_repos), 0)

    @httpretty.activate
    @patch("grimoirelab_metrics.cli.get_repository_metrics")
    def test_no_repository(self, mock_get_repository_metrics):
        """Check if it returns a warning when a package does not provide a git repository"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server()
        mock_get_repository_metrics.return_value = {"metrics": {"num_commits": 10}}

        runner = CliRunner()
        result = runner.invoke(grimoirelab_metrics, command_args("./data/missing_repo.spdx.xml", self.temp_file.name))
        self.assertEqual(result.exit_code, 0)
        self.assertIn(
            "Could not find a git repository for SPDXRef-bootstrap-gnu-config.bst-0 (bootstrap/gnu-config.bst)",
            result.output,
        )

    @httpretty.activate
    @patch("grimoirelab_metrics.cli.get_repository_metrics")
    def test_invalid_git_repository(self, mock_get_repository_metrics):
        """Check if it returns a warning when a package URI is not a valid git repository"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server()
        mock_get_repository_metrics.return_value = {"metrics": {"num_commits": 10}}

        runner = CliRunner()
        result = runner.invoke(grimoirelab_metrics, command_args("./data/invalid_repo.spdx.xml", self.temp_file.name))
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Could not find a git repository for SPDXRef-ncurses-6.40 (bootstrap/ncurses.bst)", result.output)

    @httpretty.activate
    def test_no_file(self):
        """Check if it returns an error when the file does not exist"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server()

        runner = CliRunner()
        result = runner.invoke(grimoirelab_metrics, command_args("./data/no_file.xml", self.temp_file.name))

        self.assertEqual(result.exit_code, 0)
        self.assertIn("The source is not a file and does not end with .git", result.output)
        self.assertEqual(len(http_requests), 0)
        self.assertEqual(len(http_requests_repos), 0)

    @httpretty.activate
    @patch("grimoirelab_metrics.grimoirelab_client.time.sleep")
    def test_server_error(self, mock_sleep):
        """Check if it returns a warning when there is a server error"""
        httpretty.allow_net_connect = False
        http_requests = setup_grimoirelab_error_mock_server()
        runner = CliRunner()
        result = runner.invoke(
            grimoirelab_metrics,
            command_args("./data/valid.spdx.xml", self.temp_file.name, grimoirelab_url=ERROR_GRIMOIRELAB_URL),
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Error scheduling task", result.output)
        self.assertEqual(len(http_requests), 0)

    @httpretty.activate
    @patch("grimoirelab_metrics.cli.get_repository_metrics")
    def test_never_ending_repository(self, mock_get_repository_metrics):
        """Check if it returns a warning when a repository task never ends"""

        httpretty.allow_net_connect = False
        http_requests, http_requests_repos = setup_grimoirelab_mock_server(never_ending=True)
        mock_get_repository_metrics.return_value = {"metrics": {"num_commits": 10}}

        runner = CliRunner()

        result = runner.invoke(
            grimoirelab_metrics,
            command_args("./data/valid.spdx.xml", self.temp_file.name, "--repository-timeout", "0"),
        )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Timeout waiting for repository", result.output)
        self.assertEqual(len(http_requests), 5)
        self.assertEqual(len(http_requests_repos), 10)

        with open(self.temp_file.name) as f:
            metrics = json.load(f)
        self.assertEqual(len(metrics["packages"]), 5)
        for data in metrics["packages"].values():
            self.assertIsNone(data["metrics"])


class TestGetRepository(unittest.TestCase):
    def test_valid_git_repository(self):
        valid_git_uris = [
            "https://git.myproject.org/MyProject.git",
            "http://git.myproject.org/MyProject.git",
            "git+https://git.myproject.org/MyProject.git",
            "git+http://git.myproject.org/MyProject.git",
            "git+https://git.myproject.org/MyProject.git@v1.0",
            "git://git.myproject.org/MyProject.git",
            "git://git.myproject.org/MyProject.git@master",
            "git+git://git.myproject.org/MyProject.git",
        ]

        for uri in valid_git_uris:
            with self.subTest(uri=uri):
                result = get_repository(uri)
                self.assertEqual(result, "https://git.myproject.org/MyProject.git")

    def test_invalid_git_repository(self):
        invalid_git_uris = [
            "http://git.myproject.org/MyProject",
            "git+https://git.myproject.org/MyProject",
            "svn+svn://svn.myproject.org/svn/MyProject",
            "https://git.myproject.org/MyProject/file.py",
        ]

        for uri in invalid_git_uris:
            with self.subTest(uri=uri):
                result = get_repository(uri)
                self.assertEqual(result, None)


if __name__ == "__main__":
    unittest.main()
