"""Tests for the repair fixture (``test_mean_of_values`` fails on the baseline)."""

import pytest

from statskit.stats import mean, median


def test_mean_of_values() -> None:
    assert mean([1.0, 2.0, 3.0]) == 2.0


def test_mean_of_empty_sequence_raises() -> None:
    with pytest.raises(ValueError):
        mean([])


def test_median_odd_length() -> None:
    assert median([3.0, 1.0, 2.0]) == 2.0


def test_median_even_length() -> None:
    assert median([4.0, 1.0, 3.0, 2.0]) == 2.5
