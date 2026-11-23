"""Contract tier: the model compiles.

First gate in CI and the cheapest: the Control Plane validate endpoint compiles the branch's model
without touching BigQuery, and returns the compiler errors with file names.
"""
from __future__ import annotations

import pytest

from semtest import config

pytestmark = pytest.mark.contract


def test_branch_model_compiles(control_plane) -> None:
    """Compile the branch under test (CUBE_BRANCH) or production when unset."""
    result = control_plane.validate(config.env("CUBE_BRANCH"))
    errors = "\n".join(f"  {e.get('fileName') or '?'}: {e['message']}"
                       for e in result.get("errors", []))
    assert result["valid"], f"model does not compile on {result.get('branchName')}:\n{errors}"


def test_meta_answers(storefront_meta) -> None:
    """/meta answering with entries means the environment under test is up and compiled."""
    assert storefront_meta.get("cubes"), "/meta returned nothing — is the environment active?"
