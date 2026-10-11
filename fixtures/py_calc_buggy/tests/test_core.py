"""Tests for the buggy fixture repository (one failure is intentional)."""

import pytest

from calc.core import accumulate, add, divide


def test_add() -> None:
    assert add(2, 3) == 5


def test_accumulate_empty() -> None:
    assert accumulate() == 0


def test_accumulate_values() -> None:
    assert accumulate([1, 2, 3]) == 6


def test_divide_by_zero_raises() -> None:
    with pytest.raises(ValueError):
        divide(1, 0)


def test_accumulate_known_failure() -> None:
    # Deliberately wrong expectation so the fixture always reports one failure.
    assert accumulate([1, 2]) == 4
