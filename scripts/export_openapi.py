"""Generate the frontend's API schema without starting Docker or a server."""

import json
import tempfile
from pathlib import Path

from benchmaxxing.api import create_app
from benchmaxxing.contracts import RunEvent

scratch = Path(__file__).resolve().parents[1] / "work"
scratch.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="schema-", dir=scratch) as temporary:
    schema = create_app(Path(temporary)).openapi()
    schema["components"]["schemas"]["RunEvent"] = RunEvent.model_json_schema(
        ref_template="#/components/schemas/{model}",
    )
    destination = Path(__file__).resolve().parents[1] / "web" / "openapi.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(destination)
