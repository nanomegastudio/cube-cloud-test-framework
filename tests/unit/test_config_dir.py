"""Bug #2: SEMTEST_CONFIG_DIR set in .env must be honoured (it used to be read before .env)."""
from __future__ import annotations

import pytest

from semtest import config

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A fake suite root with its own .env, and SEMTEST_CONFIG_DIR unset in the real env."""
    monkeypatch.delenv("SEMTEST_CONFIG_DIR", raising=False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path / ".env")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "suite.yml").write_text("expected_views: [example]\n")
    return tmp_path


def _team_config(root, name="team_config"):
    folder = root / name
    folder.mkdir()
    (folder / "suite.yml").write_text("expected_views: [team_view]\n")
    return folder


def test_config_dir_in_dotenv_is_used(isolated) -> None:
    """The confirmed bug: the module was already imported, .env named another folder, and the
    suite still read ./config. semtest.config is imported at the top of this file on purpose."""
    team = _team_config(isolated)
    (isolated / ".env").write_text(f"SEMTEST_CONFIG_DIR={team}\n")
    assert config.config_dir() == team
    assert config.suite()["expected_views"] == ["team_view"]


def test_relative_config_dir_is_relative_to_the_suite_root(isolated) -> None:
    _team_config(isolated)
    (isolated / ".env").write_text("SEMTEST_CONFIG_DIR=team_config\n")
    assert config.suite()["expected_views"] == ["team_view"]


def test_real_environment_wins_over_dotenv(isolated, monkeypatch) -> None:
    team = _team_config(isolated)
    other = _team_config(isolated, "other")
    (other / "suite.yml").write_text("expected_views: [other_view]\n")
    (isolated / ".env").write_text(f"SEMTEST_CONFIG_DIR={team}\n")
    monkeypatch.setenv("SEMTEST_CONFIG_DIR", str(other))
    assert config.suite()["expected_views"] == ["other_view"]


def test_missing_config_dir_is_an_error_not_an_empty_config(isolated) -> None:
    (isolated / ".env").write_text("SEMTEST_CONFIG_DIR=does_not_exist\n")
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.suite()


def test_default_is_the_bundled_config(isolated) -> None:
    assert config.suite()["expected_views"] == ["example"]
