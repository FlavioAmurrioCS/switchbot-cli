from __future__ import annotations

import logging
import subprocess
import tomllib  # type: ignore[import-not-found,unused-ignore]
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
logger = logging.getLogger(__name__)


def entrypoints() -> Generator[tuple[str, str], None, None]:
    with open("pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    yield from pyproject["project"]["scripts"].items()


@pytest.mark.parametrize("pair", entrypoints())
def test_help(pair: tuple[str, str]) -> None:
    k, _v = pair
    result = subprocess.run([k, "--help"], check=False, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        logger.error(result.stderr)
        msg = f"Error running {k} --help"
        raise AssertionError(msg)
