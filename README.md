# Benchmaxxing

A local benchmark workbench for models and agents. Prepare dependencies explicitly,
then run tasks in disposable environments with no external network. Inspect every
tool interaction, submission and grading result through a localhost UI or CLI.

**Foundation release:** one complete Python code-repair task, a deterministic
reference participant, an OpenAI-compatible model adapter, isolated Docker
execution, independent grading, durable run history, cancellation and exports.
The reference fixture validates infrastructure; it is not a broad model ranking.

## Requirements

- Windows with Docker Desktop's Linux/WSL2 engine, or Linux with Docker Engine.
- Python 3.13 and uv 0.13.7 for development/setup.
- Node 24 and npm to build the UI from source. Built wheels do not need Node.
- A local or remote OpenAI-compatible inference endpoint only when testing a model.
  Scripted runs need no GPU, model weights or API account.

## Set up from source

Run these commands from the repository root. Installation and `prepare` may use
package registries. Review Docker's availability before starting a run.

```sh
uv sync --locked
cd web
npm ci
npm run build
cd ..
uv run --locked benchmaxxing prepare
uv run --locked benchmaxxing doctor
uv run --locked benchmaxxing serve
```

Open **http://127.0.0.1:8123**. Choose the correct reference solution and start a
run. The expected score is 100%. The deliberately incorrect solution should fail.
The refusal fixture records refusal separately from task failure.

After setup, use the installed executable directly for offline operation:

```powershell
# Windows, from the prepared checkout
.\.venv\Scripts\benchmaxxing.exe serve
.\.venv\Scripts\benchmaxxing.exe run --config examples/scripted.toml
```

```sh
# Linux, from the prepared checkout
.venv/bin/benchmaxxing serve
.venv/bin/benchmaxxing run --config examples/scripted.toml
```

Alternatively, `uv run --locked --offline --no-sync benchmaxxing run` uses the
already installed environment without dependency resolution or downloads.

The default data directory is `.benchmaxxing` under the current working directory.
Pass `--data-dir /absolute/path` to each command or set `BENCHMAXXING_DATA_DIR` to
share another location. A CLI run attaches to an existing service for that directory
or starts a headless controller. Only one controller owns a directory at a time.

## Use a model

Configure an existing server that supports chat completions with tool calls.
Benchmaxxing does not install models, runtimes or GPU drivers. Edit the explicit
model ID in `examples/local-model.toml` and run:

```sh
uv run --locked --offline --no-sync benchmaxxing run --config examples/local-model.toml
```

Offline profiles accept loopback endpoints. Disable cloud forwarding in your
inference runtime and verify it with internet access disconnected; localhost alone
does not establish that inference is local.

For an already prepared Strata Qwen3.8 Flash Next instance on port 8081,
`examples/strata-qwen.toml` contains the settings used in the local reference
smoke test. See `docs/validation.md` for the result and its limits.

For a remote provider, edit `examples/api-model.toml`, set the named API-key
environment variable, and choose the API profile. Only the controller contacts
that provider. The task container still has no external network. Keys are never
copied into the task environment; configurations store the variable name only.

Model requests, responses and tool schemas are recorded, along with requested
generation settings, reported usage and model identity. Unreported information
stays unknown. A model run measures the model **with the versioned Benchmaxxing
harness**, not an independently supplied agent.

## Commands and results

| Command | Behavior |
| --- | --- |
| `doctor` | Inspect configuration, Docker access and existing preparation; no downloads. |
| `prepare` | Explicitly pull and pin the reference image and task hash. Stop the service first. |
| `run` | Execute a run specification; attach to the UI service or run headlessly. |
| `serve` | Start the loopback-only UI and API on port 8123. |
| `export RUN_ID -o result.zip` | Export a finished run with traces, artifacts and checksums. Existing files are never overwritten. |

`run` exits 0 for a passed task, 1 for task failure, and 2 for infrastructure,
unsupported capability or budget/cancellation outcomes. Ctrl+C requests cancellation.
Each repeated run creates a new ID. Interrupted runs retain evidence and become
`interrupted` when their controller restarts.

Exports contain `run.json`, `events.jsonl`, `artifacts.json`, artifact bytes, and a
SHA-256 manifest. The SQLite database remains the authority for live state and
events. Model/tool text can contain sensitive task data; exports are local files
and are not automatically published.

## Checks and packaging

See [CONTRIBUTING.md](CONTRIBUTING.md) for all test commands and
[docs/validation.md](docs/validation.md) for the verified baseline.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q
uv run --locked python scripts/export_openapi.py
cd web
npm run generate:api
npm run build
npx playwright install chromium
npm test
cd ..
uv build --no-sources
uv run --locked python scripts/check_dist.py
```

On Linux, use `npx playwright install --with-deps chromium` when browser system
libraries are not already installed. Browser installation is explicit preparation.

Build the frontend before building a wheel. The wheel includes local UI assets
and reference task files. Fresh source installations require dependency preparation;
this release is not a universal air-gapped installer or a model-weight bundle.

## Project direction

The [architecture](docs/architecture.md) documents run ownership, contracts,
isolation and recovery. The [roadmap](docs/roadmap.md) covers original/imported
benchmarks, external agents, browser and security labs, Blender, CAD, numerical
engineering, refusal analysis, reviews and comparisons.

These later milestones are not implemented in the foundation. There is no shared
hosting, distributed worker farm, managed inference runtime or universal leaderboard.

Project code is **AGPL-3.0-only**. See [LICENSE](LICENSE). Imported tasks, datasets
and tools retain their own licenses.
