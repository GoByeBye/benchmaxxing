"""The generated API schema is independent of whether UI assets were built."""

from pathlib import Path

from benchmaxxing.api import create_app


def test_unbuilt_ui_fallback_is_not_an_api_route(tmp_path, monkeypatch):
    original = Path.is_file

    def without_bundled_ui(path: Path) -> bool:
        if path.name == "index.html" and path.parent.name == "static":
            return False
        return original(path)

    monkeypatch.setattr(Path, "is_file", without_bundled_ui)
    schema = create_app(tmp_path).openapi()
    assert "/" not in schema["paths"]
    assert "/api/v1/runs" in schema["paths"]
