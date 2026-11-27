# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0
"""Deploy-time feature flags read from the environment (.env or the deploy workflow)."""

import os


def env_flag(name: str, default: bool) -> bool:
    """Return True for "true"/"yes"/"1" (any case), False for anything else, default when unset or blank."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes")
