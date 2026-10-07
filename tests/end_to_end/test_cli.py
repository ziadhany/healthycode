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
REPOSITORIES_URL = f"{GRIMOIRELAB_URL}/api/v1/ecosystems/{GRIMOIRELAB_ECOSYSTEM}" f"/projects/{GRIMOIRELAB_PROJECT}/repos/"
ERROR_REPOSITORIES_URL = (
    f"{ERROR_GRIMOIRELAB_URL}/api/v1/ecosystems/{GRIMOIRELAB_ECOSYSTEM}" f"/projects/{GRIMOIRELAB_PROJECT}/repos/"
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
                    "--from-date=2000-01-01",
                    "--to-date=2025-01-01",
                    opensearch_url=self.opensearch_url,
                    opensearch_user=self.opensearch_user,
                    opensearch_password=self.opensearch_password,
                ),
            )
            self.assertEqual(
                result.exit_code,
                0,
                msg=f"Command failed. Output:  {result.output}\nException: {result.exception}",
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
                    1,
                    msg=f"Unexpected packages in the output file: {list(metrics['packages'])}",
                )

                package = metrics["packages"]["package0"]

                self.assertEqual(
                    package["repository"],
                    "https://github.com/angular/quickstart.git",
                )

                package_metrics = package["metrics"]
                self.assertEqual(package_metrics["active_branches"], 4)
                self.assertEqual(package_metrics["casual_regular_contributors_rate"], 0.5625)
                self.assertEqual(package_metrics["coefficient_of_variation"], 5.1616276848422915)
                self.assertEqual(package_metrics["commit_size_added_lines"], 53121)
                self.assertEqual(package_metrics["commit_size_removed_lines"], 51852)
                self.assertEqual(package_metrics["commits_over_periods_rate"], 0.0)
                self.assertEqual(package_metrics["commits_per_month"], 0.5387647831800263)
                self.assertEqual(package_metrics["commits_per_week"], 0.12571178274200612)
                self.assertEqual(package_metrics["commits_per_year"], 6.554971528690319)
                self.assertEqual(package_metrics["contributor_growth"], 25)
                self.assertEqual(package_metrics["contributor_growth_rate"], 25)
                self.assertEqual(package_metrics["days_since_last_commit"], 2618)
                self.assertEqual(package_metrics["developer_categories_casual"], 9)
                self.assertEqual(package_metrics["developer_categories_core"], 3)
                self.assertEqual(package_metrics["developer_categories_regular"], 13)
                self.assertEqual(package_metrics["elephant_factor"], 2)
                self.assertEqual(package_metrics["file_types_binary"], 0)
                self.assertEqual(package_metrics["file_types_code"], 479)
                self.assertEqual(package_metrics["file_types_other"], 684)
                self.assertEqual(package_metrics["found_file_adopters"], 0)
                self.assertEqual(package_metrics["found_file_license"], 1)
                self.assertEqual(package_metrics["message_size_mean"], 59.6219512195122)
                self.assertEqual(package_metrics["message_size_median"], 46)
                self.assertEqual(package_metrics["message_size_total"], 9778)
                self.assertEqual(package_metrics["pony_factor"], 2)
                self.assertEqual(package_metrics["recent_commits"], 0)
                self.assertEqual(package_metrics["recent_contributors"], 0)
                self.assertEqual(package_metrics["recent_organizations"], 0)
                self.assertEqual(package_metrics["returning_contributors"], 0)
                self.assertEqual(package_metrics["total_commits"], 164)
                self.assertEqual(package_metrics["total_contributors"], 25)
                self.assertEqual(package_metrics["total_organizations"], 11)

                package_metadata = package["metadata"]

                self.assertEqual(
                    package_metadata["first_commit"],
                    "da1ad445ea2b8d94649f132e9f51bb73ce163264",
                )
                self.assertEqual(
                    package_metadata["first_commit_date"],
                    "2015-03-05T00:05:13-08:00",
                )
                self.assertEqual(
                    package_metadata["last_commit"],
                    "abf848628cf02fd1899ccd7b09eb7b3ffa78aa38",
                )
                self.assertEqual(
                    package_metadata["last_commit_date"],
                    "2017-10-31T16:09:38+01:00",
                )

                self.assertEqual(
                    package["score"]["metadata"],
                    {
                        "ecosystem": "npm",
                        "model": "health",
                        "version": "0.2",
                    },
                )
                self.assertEqual(package["score"]["value"], 0.0)


if __name__ == "__main__":
    unittest.main()
