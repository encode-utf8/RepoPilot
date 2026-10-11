"""Tests for the clean fixture repository (all passing, one skipped)."""

import pytest

from stringkit.text import slugify, truncate_text


def test_slugify_basic() -> None:
    assert slugify("Hello   World") == "hello-world"


def test_truncate_returns_original_when_short() -> None:
    assert truncate_text("abc", 5) == "abc"


def test_truncate_appends_ellipsis() -> None:
    assert truncate_text("abcdef", 3) == "abc..."


def test_truncate_rejects_negative_limit() -> None:
    with pytest.raises(ValueError):
        truncate_text("abc", -1)


@pytest.mark.skip(reason="unicode slug handling is not implemented yet")
def test_slugify_unicode() -> None:
    assert slugify("Ünïcode") == "unicode"
