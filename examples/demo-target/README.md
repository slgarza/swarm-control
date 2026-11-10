# Swarm tutorial target

This tiny Python project teaches the worker lifecycle. It is not a competition
benchmark or evidence of a real agent run. Its baseline has two passing tests.

| Task | Change | Owned files |
| --- | --- | --- |
| D01 | Normalize whitespace in a display name | `demo/text.py`, `tests/test_text.py` |
| D02 | Reject inverted bounds when clamping an integer | `demo/numbers.py`, `tests/test_numbers.py` |

The existing behavior is intentionally limited. `normalize_name("  Ana   López ")`
currently preserves the whitespace. D01 must return `"Ana López"`, including
tabs/newlines as whitespace, preserving case and Unicode. Empty/whitespace-only
input becomes `""`. Inputs are strings; do not introduce unrelated validation.

`clamp(value, lower, upper)` already clamps integer values inclusively. D02 must
also raise `ValueError` when `lower > upper`. Equal bounds remain valid. Inputs
are integers; do not add new numeric types or dependencies.

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q demo
```

The task JSON initially uses placeholder issue numbers 1 and 2. Before any real
run, create the matching issues in your own public GitHub repository, put the
actual numbers in those files, configure your own accounts/budget, and push the
reviewed baseline. The default profile disables remote execution and has zero
allowance. The checked-in CI workflow uses only local Python tests.

Tutorial instructions: [Swarm Control quickstart](https://github.com/slgarza/swarm-control/blob/main/docs/QUICKSTART.md)
and [real two-agent execution](https://github.com/slgarza/swarm-control/blob/main/docs/TWO_AGENT_WALKTHROUGH.md).
