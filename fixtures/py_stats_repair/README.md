# stats-fixture

A controlled fixture for RepoPilot's phase 2 repair loop.

There is a single genuine defect in `src/statskit/stats.py`: `mean` divides by
`len(values) - 1` instead of `len(values)`, so `mean([1.0, 2.0, 3.0])` returns
`3.0` and `tests/test_stats.py::test_mean_of_values` fails.

The intended repair is a one line code change. Deleting or weakening the test is
not a valid repair and must be rejected by the quality gate.
