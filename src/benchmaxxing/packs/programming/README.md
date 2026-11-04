# Original programming tasks

These 14 tasks were authored for Benchmaxxing and are licensed under
AGPL-3.0-only, like the project. They contain no imported benchmark material.
Each task has two public examples and a deliberately faulty starter, a correct
reference fixture, and an incorrect reference fixture. Expected hidden results
are literal authored data, not computed by running the correct fixture.

The registry copies only `files` and the task prompt into the candidate
workspace. `cases`, `good_solution`, and `bad_solution` are controller-side
grading and verification material. A reference fixture is a test participant,
not a model performance result.

All tasks accept one JSON value through `solve(payload)`, return JSON, and
require the input to remain unchanged. Their contracts specify ordering,
boundary behavior and tie breaking. No timing or scaling score is claimed.

| Task ID | Difficulty | Hidden checks | Incorrect fixture's main defect |
| --- | --- | ---: | --- |
| `python.topological-order` | medium | 8 | Uses a FIFO ready queue instead of selecting the current lexical minimum. |
| `python.shortest-route` | hard | 8 | Ignores improvements in hop count or path order when costs tie. |
| `python.strict-csv` | medium | 10 | Splits quoted commas and strips significant spaces; misses syntax errors. |
| `python.bracket-expansion` | medium | 8 | Reads only the first digit of a repetition count. |
| `python.weighted-schedule` | hard | 8 | Selects jobs greedily by individual profit. |
| `python.lru-cache` | medium | 8 | Does not update recency after a successful read. |
| `python.ttl-cache` | medium | 8 | Treats the expiration timestamp as still live. |
| `python.flatten-json` | medium | 8 | Does not escape object keys in JSON Pointer paths. |
| `python.prune-tree` | medium | 8 | Retains all descendants of a selected internal node. |
| `python.sequence-diff` | hard | 8 | Replaces positional mismatches instead of finding a minimal alignment. |
| `python.integer-expression` | medium | 8 | Uses floating-point division and truncation instead of integer floor division. |
| `python.json-patch` | hard | 10 | Replaces an array element when an insertion is required. |
| `python.prefix-suggestions` | medium | 8 | Uses lowercase conversion instead of Unicode case folding. |
| `python.grid-components` | medium | 9 | Connects diagonal neighbors. |

The 117 hidden checks cover empty inputs where the grammar permits them,
duplicates, replacement, exact expiration boundaries, zero-cost graph cycles,
cycle descendants, canonical ties, escaped characters, empty container leaves,
Unicode folding, integer precision, array shifts, holes, and input preservation.
Public examples are executable with `python public_tests.py` inside the task
environment. Reference fixtures must also run inside the prepared disposable
task sandbox, not on the controller host.
