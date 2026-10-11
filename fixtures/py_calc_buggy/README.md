# calc-fixture

A deliberately imperfect Python project used as a controlled fixture for RepoPilot.

Known, deterministically detectable problems:

- `F401` unused imports (`json`, `os`, `sys`)
- `F821` undefined name (`missing_helper`)
- `F841` unused local variable
- `E711` comparison to `None`
- `B006` mutable default argument
- one failing test (`test_accumulate_known_failure`)
- unpinned dependency (`urllib3>=2.0`)
