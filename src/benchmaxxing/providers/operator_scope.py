"""Persistent, workspace-local operator deferrals for explicit preparation."""

from __future__ import annotations

import json
from pathlib import Path

from benchmaxxing.environment import PreparationError


class PreparationDeferred(PreparationError):
    """The operator deferred a suite; retained preparation evidence stays valid."""


def check_preparation_scope(data_dir: Path, suite_id: str) -> None:
    """Read each checkpoint afresh so running preparation can observe a deferral.

    This only gates new preparation work. It never deletes assets, changes pins,
    stops the execution controller, or rewrites historical run results.
    """
    path = Path(data_dir) / "imports" / "operator-deferrals.json"
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return
    try:
        if len(content) > 65536:
            raise ValueError("marker exceeds 64 KiB")
        marker = json.loads(content)
        if not isinstance(marker, dict) or marker.get("schema_version") != 1:
            raise ValueError("expected schema_version 1")
        deferred = marker["deferred_suites"]
        if not isinstance(deferred, dict) or any(
            not isinstance(key, str) or not isinstance(reason, str) or not reason.strip()
            for key, reason in deferred.items()
        ):
            raise ValueError("expected suite IDs and nonempty reasons")
    except (ValueError, KeyError, TypeError) as exc:
        raise PreparationError(f"Invalid operator preparation scope at {path}: {exc}") from exc
    if suite_id in deferred:
        raise PreparationDeferred(
            f"Preparation deferred by operator for {suite_id}: {deferred[suite_id]}. "
            "Existing assets and receipts are preserved. "
            f"Review {path} before explicitly resuming preparation."
        )
