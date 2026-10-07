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
import unittest

from grimoirelab_metrics.cli import (
    grimoirelab_metrics,
    FILE_TYPE_CODE,
    FILE_TYPE_BINARY,
    DEFAULT_PONY_THRESHOLD,
    DEFAULT_ELEPHANT_THRESHOLD,
    DEFAULT_DEV_CATEGORIES_THRESHOLDS,
)
from end_to_end.base import EndToEndTestCase

GRIMOIRELAB_URL = "http://localhost:8000"
GRIMOIRELAB_USER = "admin"
GRIMOIRELAB_PASSWORD = "admin"
GRIMOIRELAB_ECOSYSTEM = "npm-training-set"
GRIMOIRELAB_PROJECT = "npm-popular-components"
OPENSEARCH_INDEX = "events"
FROM_DATE = datetime.datetime(2000, 1, 1)

ERROR_GRIMOIRELAB_URL = "http://localhost:8001"
REPOSITORIES_URL = (
    f"{GRIMOIRELAB_URL}/api/v1/ecosystems/{GRIMOIRELAB_ECOSYSTEM}"
    f"/projects/{GRIMOIRELAB_PROJECT}/repos/"
)
ERROR_REPOSITORIES_URL = (
    f"{ERROR_GRIMOIRELAB_URL}/api/v1/ecosystems/{GRIMOIRELAB_ECOSYSTEM}"
    f"/projects/{GRIMOIRELAB_PROJECT}/repos/"
)


def command_args(
    source,
    output_path,
    *extra,
    grimoirelab_url=GRIMOIRELAB_URL,
    opensearch_url=None,
    opensearch_user=None,
    opensearch_password=None,
    opensearch_index=OPENSEARCH_INDEX,
):
    """Build the invocation exactly like collect_and_store_grimoire_metric().

    The OpenSearch server started by EndToEndTestCase runs on a random
    port, so its connection parameters cannot be hardcoded: they must
    be taken from the test case and passed to this function.
    """
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
        opensearch_url,
        "--opensearch-index",
        opensearch_index,
        "--opensearch-user",
        opensearch_user,
        "--opensearch-password",
        opensearch_password,
        "--output",
        output_path,
        *extra,
    ]


class TestMetrics(EndToEndTestCase):
    """End to end tests for grimoirelab metrics CLI"""

    def test_metrics(self):
        """Check whether the metrics are correctly calculated"""

        with self.assertLogs(logging.getLogger()) as logger:
            result = self.runner.invoke(
                grimoirelab_metrics,
                command_args(
                    "https://github.com/angular/quickstart.git",
                    self.temp_file.name,
                    opensearch_url=self.opensearch_url,
                    opensearch_user=self.opensearch_user,
                    opensearch_password=self.opensearch_password,
                ),
            )
            self.assertEqual(
                result.exit_code,
                0,
                msg=f"Command failed. Output: {result.output}\nException: {result.exception}",
            )

            self.assertIn("INFO:root:Found 1 git repositories", logger.output)
            self.assertIn("INFO:root:Scheduling data collection tasks with GrimoireLab", logger.output)
            self.assertIn("INFO:root:Data collection finished for 1/1 repositories", logger.output)
            self.assertTrue(
                any(
                    message.startswith("INFO:root:Metrics and scores are calculated and written to file")
                    for message in logger.output
                ),
                msg=f"Metrics calculation message not found in: {logger.output}",
            )

            with open(self.temp_file.name) as f:
                metrics = json.load(f)
                self.assertEqual(
                    len(metrics["packages"]),
                    2,
                    msg=f"Unexpected packages in the output file: {list(metrics['packages'])}",
                )

                self.assertIn("SPDXRef-angular", metrics["packages"])
                self.assertEqual(
                    metrics["packages"]["SPDXRef-angular"]["repository"], "https://github.com/angular/quickstart"
                )
                quickstart_metrics = metrics["packages"]["SPDXRef-angular"]["metrics"]
                self.assertEqual(quickstart_metrics["total_commits"], 164)
                self.assertEqual(quickstart_metrics["total_contributors"], 25)
                self.assertEqual(quickstart_metrics["pony_factor"], 2)
                self.assertEqual(quickstart_metrics["elephant_factor"], 2)
                self.assertEqual(quickstart_metrics["file_types_other"], 684)
                self.assertEqual(quickstart_metrics["file_types_binary"], 0)
                self.assertEqual(quickstart_metrics["file_types_code"], 479)
                self.assertEqual(quickstart_metrics["commit_size_added_lines"], 53121)
                self.assertEqual(quickstart_metrics["commit_size_removed_lines"], 51852)
                self.assertEqual(quickstart_metrics["message_size_total"], 9778)
                self.assertAlmostEqual(quickstart_metrics["message_size_mean"], 59.6219, delta=0.1)
                self.assertEqual(quickstart_metrics["message_size_median"], 46)
                self.assertEqual(quickstart_metrics["developer_categories_core"], 3)
                self.assertEqual(quickstart_metrics["developer_categories_regular"], 13)
                self.assertEqual(quickstart_metrics["developer_categories_casual"], 9)

                elapsed_days = (datetime.datetime.now() - FROM_DATE).days
                self.assertAlmostEqual(quickstart_metrics["commits_per_week"], 164 / (elapsed_days / 7), delta=0.1)
                self.assertAlmostEqual(quickstart_metrics["commits_per_month"], 164 / (elapsed_days / 30), delta=0.1)
                self.assertAlmostEqual(quickstart_metrics["commits_per_year"], 164 / (elapsed_days / 365), delta=0.1)

                # First and last commit metrics
                self.assertEqual(
                    metrics["packages"]["SPDXRef-angular"]["metadata"]["first_commit"],
                    "da1ad445ea2b8d94649f132e9f51bb73ce163264",
                )
                self.assertEqual(
                    metrics["packages"]["SPDXRef-angular"]["metadata"]["last_commit"],
                    "abf848628cf02fd1899ccd7b09eb7b3ffa78aa38",
                )
                self.assertEqual(
                    metrics["packages"]["SPDXRef-angular"]["metadata"]["first_commit_date"],
                    "2015-03-05T00:05:13-08:00",
                )
                self.assertEqual(
                    metrics["packages"]["SPDXRef-angular"]["metadata"]["last_commit_date"],
                    "2017-10-31T16:09:38+01:00",
                )


if __name__ == "__main__":
    unittest.main()
