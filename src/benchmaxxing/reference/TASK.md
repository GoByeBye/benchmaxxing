# Repair interval merging

Implement `merge_intervals(intervals)` in `solution.py`.

The input is a list of valid, closed integer intervals `[start, end]`, where
`start <= end`. Return a list sorted by start, merging all overlapping intervals,
including intervals sharing an endpoint. Disjoint intervals stay separate.
Return `[]` for empty input. Do not mutate the input list or its nested lists.

Examples:

- `[[1, 3], [2, 6], [8, 10]]` becomes `[[1, 6], [8, 10]]`.
- `[[4, 5], [1, 2], [2, 3]]` becomes `[[1, 3], [4, 5]]`.

Run the public examples with `python public_tests.py`. Submit the final
`solution.py`. The final evaluator checks additional inputs independently.
This reference task validates the benchmark pipeline; it is not a model ranking.
