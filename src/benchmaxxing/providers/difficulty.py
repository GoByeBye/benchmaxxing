"""Public difficulty labels derived from immutable upstream task identities."""

from __future__ import annotations

import re

from benchmaxxing.contracts import Difficulty

_CYBENCH_LEVEL = re.compile(r"^\[\s*(very easy|easy|medium|hard|very hard|insane)\s*\]", re.I)


def display_difficulty(suite_id: str, upstream_id: str, recorded: Difficulty) -> Difficulty:
    """Correct legacy Cybench projections without changing hashed import records."""
    if suite_id != "cybench":
        return recorded
    name = upstream_id.rsplit("/", 1)[-1]
    match = _CYBENCH_LEVEL.match(name)
    if match is None:
        return "unrated"
    level = match.group(1).casefold()
    if level in {"very easy", "easy"}:
        return "easy"
    if level == "medium":
        return "medium"
    return "hard"
