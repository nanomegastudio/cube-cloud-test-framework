"""Static lint for a Cube model directory. Runs in seconds, needs no Cube and no BigQuery.

Checks:
  1. Every model file renders as Jinja and parses as YAML with DUPLICATE KEYS REJECTED.
     PyYAML silently keeps the last duplicate; Cube's parser refuses the whole model at boot.
  2. No quoted Jinja interpolation like  sql_table: "{{ ... }}"  (Cube JSON-quotes interpolated
     values, so the quotes double up and the YAML breaks).
  3. Every cube is `public: false`; only views are public.
  4. Every cube declares a primary_key dimension (without one, joins can fan out silently).

Jinja globals from model/globals.py are not importable outside Cube, so unknown names render
as empty strings here. That is enough to check structure; compilation proper is the Control
Plane validate endpoint (tests/contract/test_compile.py).

Usage:  python scripts/lint_model.py path/to/cube-repo/model
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

QUOTED_JINJA = re.compile(r""":\s*["']\{\{[^}]*\}\}["']""")


class LenientUndefined(ChainableUndefined):
    """Unknown names (Python globals from model/globals.py) render as '' and are callable."""

    def __call__(self, *args, **kwargs):
        return self


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that raises on a duplicated mapping key, like Cube does."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicated key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def lint(model_dir: pathlib.Path) -> list[str]:
    env = Environment(loader=FileSystemLoader(str(model_dir)), undefined=LenientUndefined)
    problems: list[str] = []
    files = sorted(p for p in model_dir.rglob("*") if p.suffix in {".yml", ".yaml"})
    if not files:
        return [f"no .yml files under {model_dir}"]
    for path in files:
        rel = path.relative_to(model_dir).as_posix()
        raw = path.read_text(encoding="utf-8")
        for n, line in enumerate(raw.splitlines(), 1):
            if QUOTED_JINJA.search(line):
                problems.append(f"{rel}:{n}: quoted Jinja interpolation — remove the quotes")
        try:
            rendered = env.get_template(rel).render()
            doc = yaml.load(rendered, Loader=UniqueKeyLoader) or {}
        except Exception as exc:  # noqa: BLE001 — report every file, do not stop at the first
            problems.append(f"{rel}: {type(exc).__name__}: {exc}")
            continue
        for cube in doc.get("cubes") or []:
            name = cube.get("name", "?")
            if cube.get("public") is not False:
                problems.append(f"{rel}: cube {name} is not `public: false`")
            dims = cube.get("dimensions") or []
            if not any(d.get("primary_key") for d in dims if isinstance(d, dict)):
                problems.append(f"{rel}: cube {name} declares no primary_key dimension")
    return problems


def main() -> int:
    model_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "model").resolve()
    problems = lint(model_dir)
    for p in problems:
        print(f"::error::{p}" if "GITHUB_ACTIONS" in os.environ else p)
    print(f"{len(problems)} problem(s) in {model_dir}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
