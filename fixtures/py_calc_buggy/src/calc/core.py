"""Arithmetic helpers containing intentional, known defects."""

from __future__ import annotations

from typing import Any

import json
import os
import sys


def add(left: int, right: int) -> int:
    return left + right


def divide(left: float, right: float) -> float:
    if right == 0:
        raise ValueError("division by zero")
    return left / right


def accumulate(values: list[int] = []) -> int:
    total = 0
    for value in values:
        total += value
    return total


def unused_local() -> int:
    leftover = 42
    return 0


def broken_reference() -> Any:
    return missing_helper()


def compare_with_none(value: Any) -> bool:
    return value == None
