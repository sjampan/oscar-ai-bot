# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""Feature flags that choose which agent implementations app.py deploys."""

import pytest

from utils.feature_flags import env_flag


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("TRUE", True), (" yes ", True), ("1", True),
    ("false", False), ("0", False), ("no", False), ("anything-else", False),
])
def test_explicit_values(monkeypatch, value, expected):
    monkeypatch.setenv("OSCAR_TEST_FLAG", value)
    assert env_flag("OSCAR_TEST_FLAG", default=not expected) is expected


@pytest.mark.parametrize("default", [True, False])
def test_unset_or_blank_uses_default(monkeypatch, default):
    monkeypatch.delenv("OSCAR_TEST_FLAG", raising=False)
    assert env_flag("OSCAR_TEST_FLAG", default) is default
    monkeypatch.setenv("OSCAR_TEST_FLAG", "  ")
    assert env_flag("OSCAR_TEST_FLAG", default) is default
