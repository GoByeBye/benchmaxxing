"""Operator deferral must stop preparation without losing completed evidence."""

import json
from pathlib import Path

import pytest

from benchmaxxing.environment import PreparationError
from benchmaxxing.providers import operator_scope, registry


def defer(data_dir: Path) -> Path:
    marker = registry.root(data_dir) / "operator-deferrals.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"schema_version": 1, "deferred_suites": {"frontend-design": "Scope changed"}}),
        encoding="utf-8",
    )
    return marker


def test_scope_is_reread_and_does_not_gate_unrelated_suites(tmp_path):
    operator_scope.check_preparation_scope(tmp_path, "frontend-design")
    marker = defer(tmp_path)
    operator_scope.check_preparation_scope(tmp_path, "miniwob")
    with pytest.raises(operator_scope.PreparationDeferred, match="Scope changed"):
        operator_scope.check_preparation_scope(tmp_path, "frontend-design")
    marker.unlink()
    operator_scope.check_preparation_scope(tmp_path, "frontend-design")


@pytest.mark.parametrize("content", ["{", "[]", '{"schema_version":1}', "x" * 65537])
def test_invalid_scope_stops_preparation(tmp_path, content):
    marker = defer(tmp_path)
    marker.write_text(content)
    with pytest.raises(PreparationError, match="Invalid operator preparation scope"):
        operator_scope.check_preparation_scope(tmp_path, "frontend-design")
