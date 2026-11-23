"""Load the suite's YAML config and environment variables.

Everything project-specific (expected views, personas, row-filter rules, golden numbers, rollups)
lives in config/*.yml so the test code itself never needs editing for a new view or persona.
"""
from __future__ import annotations

import os
import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOTENV_PATH = ROOT / ".env"


class ConfigError(Exception):
    """The suite's configuration is missing or points somewhere that does not exist."""


def load_dotenv(path: pathlib.Path | None = None) -> None:
    """Load KEY=VALUE lines from .env for any key not already set (real env vars win).

    Safe to call more than once: a key that is already set is never overwritten.
    """
    path = DOTENV_PATH if path is None else path
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def config_dir() -> pathlib.Path:
    """The folder holding suite.yml, personas.yml and the case files.

    Resolved on every call, after loading .env, so SEMTEST_CONFIG_DIR works whether it is set in
    the real environment or in .env. (It used to be read once at import time, before .env was
    loaded, so a value in .env was silently ignored and the example config ran instead.)
    A relative path is taken relative to the suite's root folder, where .env lives. A folder that
    does not exist is an error, never an empty config.
    """
    load_dotenv()
    raw = os.environ.get("SEMTEST_CONFIG_DIR")
    if not raw:
        return ROOT / "config"
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_dir():
        raise ConfigError(f"SEMTEST_CONFIG_DIR={raw!r} does not exist (resolved to {path})")
    return path


def load_yaml(name: str) -> dict[str, Any]:
    """Read <config dir>/<name>; a missing file is an empty config, not an error."""
    path = config_dir() / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, treating empty strings as unset."""
    value = os.environ.get(name)
    return value if value else default


def suite() -> dict[str, Any]:
    """The main suite config (storefront, governance rules, row filters, fan-out allowlist)."""
    return load_yaml("suite.yml")


def personas() -> dict[str, dict[str, Any]]:
    """Persona name -> {claims, can_see, cannot_see, row_values}."""
    return load_yaml("personas.yml").get("personas", {})


def parity_cases() -> list[dict[str, Any]]:
    """Golden-number and BigQuery parity cases."""
    return load_yaml("parity_cases.yml").get("cases", [])


def rollup_cases() -> list[dict[str, Any]]:
    """Pre-aggregation routing / materialisation / value / freshness cases."""
    return load_yaml("rollup_cases.yml").get("cases", [])
