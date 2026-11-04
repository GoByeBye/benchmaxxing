"""Controller-owned BrowserGym JSONL worker (optional packages live in its image).

The participant receives observations and a bounded action vocabulary, never a
Python interpreter, a filesystem tool, or the private grader configuration. This
process must run in an owned container with enforced task-local/no networking.
It intentionally cannot download dependencies or reset a shared website instance.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import importlib
import io
import json
import math
import os
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

RESULT_PATH = Path("/tmp/benchmaxxing-grade/result.json")
MAX_REQUEST_BYTES = 131072
MAX_TEXT_BYTES = 1048576
MAX_IMAGE_BYTES = 4194304

# Required keys followed by optional keys. Values are separately type-checked.
_ACTIONS: dict[str, tuple[set[str], set[str]]] = {
    "noop": (set(), {"wait_ms"}),
    "fill": ({"bid", "value"}, {"enable_autocomplete_menu"}),
    "click": ({"bid"}, {"button", "modifiers"}),
    "dblclick": ({"bid"}, {"button", "modifiers"}),
    "hover": ({"bid"}, set()),
    "focus": ({"bid"}, set()),
    "clear": ({"bid"}, set()),
    "press": ({"bid", "key_comb"}, set()),
    "select_option": ({"bid", "options"}, set()),
    "scroll": ({"delta_x", "delta_y"}, set()),
    "mouse_move": ({"x", "y"}, set()),
    "mouse_click": ({"x", "y"}, {"button", "modifiers"}),
    "mouse_dblclick": ({"x", "y"}, {"button", "modifiers"}),
    "keyboard_press": ({"key"}, set()),
    "keyboard_type": ({"text"}, set()),
    "goto": ({"url"}, set()),
    "go_back": (set(), set()),
    "go_forward": (set(), set()),
    "new_tab": (set(), set()),
    "tab_close": (set(), set()),
    "tab_focus": ({"index"}, set()),
    "send_msg_to_user": ({"text"}, set()),
    "report_infeasible": ({"reason"}, set()),
}


def _url_allowed(url: str, config: dict[str, Any]) -> bool:
    if url in ("about:blank", "about:newtab", "chrome://newtab/"):
        return True
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        return False
    if config.get("suite_id") == "miniwob":
        expected = config.get("miniwob_base_url", "") + str(config.get("task_id")) + ".html"
        return parsed.scheme == "file" and url.split("#", 1)[0] == expected
    allowed = {
        (urlsplit(value).scheme, urlsplit(value).netloc)
        for value in config.get("url_map", {}).values()
    }
    return parsed.scheme in ("http", "https") and (parsed.scheme, parsed.netloc) in allowed


def action_code(name: Any, arguments: Any, config: dict[str, Any]) -> str:
    """Build one escaped BrowserGym call, never evaluate participant Python."""
    if not isinstance(name, str) or name not in _ACTIONS or not isinstance(arguments, dict):
        raise ValueError("Expected a supported browser action and an arguments object")
    required, optional = _ACTIONS[name]
    if not required.issubset(arguments) or not set(arguments).issubset(required | optional):
        raise ValueError("Browser action has missing or unexpected arguments")
    if len(json.dumps(arguments, allow_nan=False).encode()) > MAX_REQUEST_BYTES // 2:
        raise ValueError("Browser action is too large")
    for key, value in arguments.items():
        if key in ("x", "y", "delta_x", "delta_y", "wait_ms"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Coordinates and durations must be finite numbers")
            if not math.isfinite(value) or abs(value) > 100000:
                raise ValueError("Coordinate or duration is outside the supported range")
            if key == "wait_ms" and not 0 <= value <= 10000:
                raise ValueError("noop may wait at most 10000 ms")
        elif key == "index":
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError("Tab index must be an integer between 0 and 100")
        elif key == "enable_autocomplete_menu":
            if not isinstance(value, bool):
                raise ValueError("Autocomplete flag must be boolean")
        elif key == "modifiers":
            if not isinstance(value, list) or any(
                item not in ("Alt", "Control", "ControlOrMeta", "Meta", "Shift") for item in value
            ):
                raise ValueError("Invalid key modifiers")
        elif key == "options":
            if not isinstance(value, str) and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                raise ValueError("Select options must be text or a list of text values")
        elif not isinstance(value, str):
            raise ValueError("Browser text arguments must be strings")
        if key == "button" and value not in ("left", "middle", "right"):
            raise ValueError("Invalid mouse button")
    if name == "goto" and not _url_allowed(arguments["url"], config):
        raise ValueError("Navigation is restricted to the prepared task websites")
    return name + "(" + ", ".join(f"{key}={value!r}" for key, value in arguments.items()) + ")"


def _offline_environment() -> None:
    for name in list(os.environ):
        if name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            del os.environ[name]
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "WANDB_MODE": "disabled",
        }
    )


def preflight(config: dict[str, Any]) -> None:
    """Fail before imports with known auto-download side effects."""
    _offline_environment()
    suite = config.get("suite_id")
    if suite not in ("miniwob", "webarena-verified", "visualwebarena"):
        raise ValueError("This offline worker does not support hosted WorkArena")
    if config.get("schema_version") != 1:
        raise ValueError("Unsupported browser worker configuration version")
    if suite == "miniwob":
        base = urlsplit(config.get("miniwob_base_url", ""))
        if base.scheme != "file" or base.netloc or not base.path.startswith("/runtime/"):
            raise ValueError("MiniWoB requires prepared local HTML beneath /runtime")
        if not Path(base.path, str(config.get("task_id")) + ".html").is_file():
            raise ValueError("Missing prepared MiniWoB HTML; run explicit dependency preparation")
    else:
        # A controller must issue this only after cloning/resetting its owned
        # services. The worker never resets an arbitrary shared URL itself.
        receipt = config.get("reset_receipt", {})
        if (
            not config.get("attempt_id")
            or not isinstance(receipt, dict)
            or (
                receipt.get("attempt_id") != config["attempt_id"]
                or receipt.get("fresh_owned_services") is not True
            )
        ):
            raise ValueError("A per-attempt owned website reset receipt is required")
        urls = config.get("url_map")
        if (
            not isinstance(urls, dict)
            or not urls
            or any(
                not isinstance(value, str) or urlsplit(value).scheme not in ("http", "https")
                for value in urls.values()
            )
        ):
            raise ValueError("Prepared task-local URL map is required")
        nltk = importlib.import_module("nltk")
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError as exc:
            raise ValueError(
                "Prepare NLTK punkt_tab explicitly before running this adapter"
            ) from exc
        prefix = "VWA_" if suite == "visualwebarena" else "WA_"
        for name, value in urls.items():
            os.environ[prefix + name.upper()] = value
        if suite == "visualwebarena":
            raw = Path(config["task_config_path"]).read_text()
            if any(key in raw for key in ('"fuzzy_match"', '"ua_match"')):
                raise ValueError(
                    "External native model judges are disabled in this offline variant"
                )
            # local_files_only + process-wide offline flags forbid implicit downloads.
            hub = importlib.import_module("huggingface_hub")
            hub.snapshot_download("Salesforce/blip2-flan-t5-xl", local_files_only=True)
    playwright = importlib.import_module("playwright.sync_api")
    with playwright.sync_playwright() as browser:
        if not Path(browser.chromium.executable_path).is_file():
            raise ValueError("Prepared Chromium is missing; run explicit browser preparation")


def create_environment(config: dict[str, Any]) -> Any:
    preflight(config)
    env_module = importlib.import_module("browsergym.core.env")
    actions = importlib.import_module("browsergym.core.action.highlevel")
    suite = config["suite_id"]
    if suite == "miniwob":
        miniwob = importlib.import_module("browsergym.miniwob")
        task_type = next(
            (task for task in miniwob.ALL_MINIWOB_TASKS if task.subdomain == config["task_id"]),
            None,
        )
        if task_type is None:
            raise ValueError("Task is not registered in the pinned MiniWoB adapter")
        kwargs = {"base_url": config["miniwob_base_url"]}
    else:
        module_name, class_name = (
            ("browsergym.webarena_verified.task", "WebArenaVerifiedTask")
            if suite == "webarena-verified"
            else ("browsergym.visualwebarena.task", "GenericVisualWebArenaTask")
        )
        base = getattr(importlib.import_module(module_name), class_name)
        raw = Path(config["task_config_path"]).read_text(encoding="utf-8")
        for key, value in config["url_map"].items():
            raw = raw.replace("__" + key.upper() + "__", value)
        record = json.loads(raw)
        images = record.get("image")
        if images:
            values = [images] if isinstance(images, str) else images
            replacements = config.get("image_map", {})
            prepared = []
            for value in values:
                local = replacements.get(value)
                if not isinstance(local, str) or not local.startswith("/runtime/"):
                    raise ValueError(
                        "Every visual goal image needs an explicit local prepared mapping"
                    )
                if not Path(local).is_file():
                    raise ValueError("Prepared visual goal image is missing")
                prepared.append(local)
            record["image"] = prepared[0] if isinstance(images, str) else prepared

        def initialize(self: Any, seed: int, **arguments: Any) -> None:
            base.__init__(self, seed=seed, **arguments)
            # Use the captured record, not potentially changed package metadata.
            self.task_configs = [record]

        task_type = type("PreparedBrowserTask", (base,), {"__init__": initialize})
        kwargs = {"task_id": config["task_id"]}
    action_set = actions.HighLevelActionSet(
        subsets=["chat", "infeas", "bid", "coord", "nav", "tab"], multiaction=False
    )
    return env_module.BrowserEnv(
        task_entrypoint=task_type,
        task_kwargs=kwargs,
        headless=True,
        action_mapping=action_set.to_python_code,
    )


def public_observation(observation: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Allowlist only candidate-visible observations; never forward info/reward."""
    urls = observation.get("open_pages_urls", [])
    if any(not _url_allowed(url, config) for url in urls):
        raise ValueError("Browser left the allowed task websites")
    result: dict[str, Any] = {}
    for key in (
        "goal",
        "url",
        "open_pages_urls",
        "open_pages_titles",
        "focused_element_bid",
        "last_action",
        "last_action_error",
        "axtree_object",
    ):
        if key in observation:
            result[key] = observation[key]
    if "active_page_index" in observation:
        active = observation["active_page_index"]
        result["active_page_index"] = int(active.item() if hasattr(active, "item") else active)
    encoded = json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)
    if len(encoded.encode()) > MAX_TEXT_BYTES:
        # Keep goal and navigation intact; record loss of oversized tree explicitly.
        tree = str(result.pop("axtree_object", ""))
        result["axtree_text_truncated"] = tree[: MAX_TEXT_BYTES // 4]
        result["observation_truncated"] = True
        if len(json.dumps(result).encode()) > MAX_TEXT_BYTES:
            raise ValueError("Browser observation exceeds the bounded text budget")
    image = observation.get("screenshot")
    if image is not None:
        pillow = importlib.import_module("PIL.Image")
        buffer = io.BytesIO()
        pillow.fromarray(image).save(buffer, format="PNG")
        payload = buffer.getvalue()
        if len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("Browser screenshot exceeds the bounded image budget")
        result["screenshot_png_base64"] = base64.b64encode(payload).decode("ascii")
    # BrowserGym's structured goal may contain input images; include only actual
    # text/image fields, never arbitrary extra metadata from a task adapter.
    goal_object = observation.get("goal_object", [])
    if isinstance(goal_object, (list, tuple)):
        safe_goal: list[dict[str, Any]] = []
        for item in goal_object:
            if isinstance(item, dict) and item.get("type") == "text":
                safe_goal.append({"type": "text", "text": str(item.get("text", ""))})
            elif isinstance(item, dict) and item.get("type") == "image_url":
                image_url = item.get("image_url", {})
                url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
                if isinstance(url, str) and url.startswith("data:image/"):
                    safe_goal.append({"type": "image_url", "image_url": {"url": url}})
        if len(json.dumps(safe_goal).encode()) > MAX_IMAGE_BYTES * 2:
            raise ValueError("Browser goal images exceed the bounded image budget")
        result["goal_object"] = safe_goal
    result["available_actions"] = {
        name: {"required": sorted(required), "optional": sorted(optional)}
        for name, (required, optional) in _ACTIONS.items()
    }
    return result


class BrowserSession:
    def __init__(self, config: dict[str, Any], result_path: Path = RESULT_PATH) -> None:
        self.config = config
        self.result_path = result_path
        self.env: Any = None
        self.observation: dict[str, Any] = {}
        self.reward: float | None = None
        self.terminated = False
        self.truncated = False
        self.finalized = False
        self.steps = 0

    def _step(self, code: str) -> None:
        observation, reward, terminated, truncated, _private_info = self.env.step(code)
        if isinstance(reward, bool) or not isinstance(reward, (int, float)):
            # numpy floating values implement __float__, but bool is never a score.
            if isinstance(reward, bool):
                raise ValueError("Native browser reward is not numeric")
            reward = float(reward)
        if not math.isfinite(reward) or not 0 <= reward <= 1:
            raise ValueError("Native browser reward is outside [0,1]")
        self.observation, self.reward = observation, float(reward)
        self.terminated, self.truncated = bool(terminated), bool(truncated)
        self.steps += 1

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("op")
        if operation == "close":
            if self.env is not None:
                self.env.close()
            return {"closed": True}
        if operation == "reset":
            if self.env is not None:
                raise ValueError("A benchmark attempt permits exactly one reset")
            self.env = create_environment(self.config)
            self.observation, _private_info = self.env.reset(seed=int(self.config.get("seed", 0)))
            return {"observation": public_observation(self.observation, self.config)}
        if self.env is None:
            raise ValueError("Reset the prepared task before observing or acting")
        if operation == "observe":
            # BrowserGym 0.14.x exposes this extraction method; refreshing must
            # not execute an action or invoke the evaluator a second time.
            self.observation = self.env._get_obs()
            return {
                "observation": public_observation(self.observation, self.config),
                "terminated": self.terminated,
                "truncated": self.truncated,
            }
        if self.finalized:
            raise ValueError("The benchmark attempt has already been finalized")
        if operation == "action":
            if self.terminated or self.truncated:
                raise ValueError("The native browser task has ended; submit the result")
            self._step(action_code(request.get("name"), request.get("arguments"), self.config))
            return {
                "observation": public_observation(self.observation, self.config),
                "terminated": self.terminated,
                "truncated": self.truncated,
            }
        if operation == "finalize":
            response = request.get("response", "")
            if not isinstance(response, str):
                raise ValueError("Final response must be text")
            if not self.terminated and not self.truncated:
                self._step(action_code("send_msg_to_user", {"text": response}, self.config))
            if self.reward is None:
                raise ValueError("Native evaluator did not produce a score")
            result = {
                "schema_version": 1,
                "passed": self.reward == 1.0,
                "score": self.reward,
                "checks": [
                    {
                        "name": "native_browser_evaluation",
                        "passed": self.reward == 1.0,
                        "detail": f"Native BrowserGym reward: {self.reward}",
                    }
                ],
                "evidence": {
                    "suite_id": self.config["suite_id"],
                    "task_id": self.config["task_id"],
                    "seed": self.config.get("seed", 0),
                    "steps": self.steps,
                    "terminated": self.terminated,
                    "truncated": self.truncated,
                },
            }
            self.result_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.result_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
            temporary.replace(self.result_path)
            self.finalized = True
            return {"finalized": True, "terminated": self.terminated}
        raise ValueError("Unsupported browser worker operation")


def serve(session: BrowserSession, source: TextIO, destination: TextIO) -> None:
    while True:
        line = source.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return
        request: dict[str, Any] = {}
        try:
            if len(line.encode()) > MAX_REQUEST_BYTES or not line.endswith("\n"):
                raise ValueError("Request exceeds the bounded JSONL limit")
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("JSONL requests must be objects")
            request = parsed
            # Upstream stdout/logging must not corrupt protocol framing.
            with contextlib.redirect_stdout(sys.stderr):
                response = session.request(request)
            result = {"id": request.get("id"), "ok": True, **response}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            message = str(exc) if isinstance(exc, ValueError) else "Browser worker operation failed"
            result = {"id": request.get("id"), "ok": False, "error": message[:1000]}
        destination.write(
            json.dumps(result, ensure_ascii=False, default=str, allow_nan=False) + "\n"
        )
        destination.flush()
        if request.get("op") == "close" or len(line.encode()) > MAX_REQUEST_BYTES:
            return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--grade", type=Path)
    arguments = parser.parse_args()
    if arguments.grade:
        result = json.loads(arguments.grade.read_text(encoding="utf-8"))
        score = result.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("No valid authoritative browser result is available")
        if result.get("passed") is not (score == 1.0):
            raise ValueError("Inconsistent authoritative browser result")
        print(json.dumps(result, allow_nan=False))
        return
    if arguments.config is None:
        parser.error("--config or --grade is required")
    config = json.loads(arguments.config.read_text(encoding="utf-8"))
    session = BrowserSession(config)
    try:
        serve(session, sys.stdin, sys.stdout)
    finally:
        if session.env is not None:
            session.env.close()


if __name__ == "__main__":
    main()
