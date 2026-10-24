"""Opt-in live tests and deterministic call cleanup."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from .core import CallResult, call


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="enable live SIP tests")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: requires --live and SIPLING_TEST_URI")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--live"):
        for item in items:
            fixtures = item.fixturenames if isinstance(item, pytest.Function) else ()
            if "live" in item.keywords and "earshot_transport" not in fixtures:
                item.add_marker(pytest.mark.skip(reason="live SIP tests require --live"))


@pytest.fixture
def sip_caller() -> Iterator[Callable[..., CallResult]]:
    results: list[CallResult] = []

    def dial(uri: str, **kwargs: Any) -> CallResult:
        result = call(uri, **kwargs)
        results.append(result)
        return result

    yield dial
    for result in reversed(results):
        result.hangup()
