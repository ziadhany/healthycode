# -*- coding: utf-8 -*-
#
# Copyright (C) AboutCode
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

import math

from typing import Dict

# These coefficients were calculated with the notebooks and data available
# at https://github.com/aboutcode-org/healthycode/blob/main/model/npm/README.md


class npmModel:

    # Ecosystem Name
    ECOSYSTEM_NAME = "npm"
    # Model Name
    MODEL_NAME = "health"
    # Model Version
    MODEL_VERSION = "0.2"

    # We have dropped the low-impact metrics, those with a coefficient close to 0
    COEFFICIENTS = {
        'active_branches': -0.834069,
        'commits_over_periods_rate': -0.841944,
        'commit_size_added_lines': -0.389225,
        'contributor_growth_rate': 0.114363,
        'days_since_last_commit': 1.081051,
        'developer_categories_casual': 0.958613,
        'developer_categories_core': -1.277484,
        'developer_categories_regular': 0.193106,
        'file_types_code': -0.905957,
        'found_file_license': 0.376334,
        'returning_contributors': -1.167053,
    }
    # Model Intercept
    Z = -1.023426

    def __init__(self):
        self.coefficients = self.COEFFICIENTS.copy()
        self.z = self.Z

    def calculate_score(self, metrics: Dict[str, float]) -> float:
        """
        Calculates the probability of a repository being 'Unhealthy' based on
        the pruned logistic regression model metrics.
        Parameters:
        metrics (dict): Dictionary containing the project feature names and values.
        Returns:
        float: Probability score between 0.0 (Healthy) and 1.0 (Unhealthy).
        """

        z = self.z
        # Calculate the linear combination (log-odds)
        for metric, coef in self.coefficients.items():
            # FIXME. We set by default 0 if a metric is missing. Is this safe?
            value = metrics.get(metric, 0.0)
            z += coef * value
        # Apply the Sigmoid function to get the final probability
        try:
            probability = 1 / (1 + math.exp(-z))
        except OverflowError:
            # Safeguard against extreme values of z
            # FIXME Is this correct?
            probability = 0.0 if z < 0 else 1.0
        return {
            "value": probability,
            "metadata": {
                "ecosystem": self.ECOSYSTEM_NAME,
                "model": self.MODEL_NAME,
                "version": self.MODEL_VERSION
            }
        }
