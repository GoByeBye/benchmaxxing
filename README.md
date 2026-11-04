# Benchmaxxing

A local benchmark workbench for models and agents. Prepare dependencies explicitly,
then run tasks in disposable environments with no external network. Inspect every
tool interaction, submission and grading result through a localhost UI or CLI.

**126 installed original tasks:** 82 workspace tasks in programming, numerical
engineering, defensive security, terminal workflows and synthetic policy decisions,
plus 44 offline knowledge and long-context retrieval profiles. Together they have
961 grading checks (681 workspace checks and 280 answer checks). Use a deterministic
reference participant or an OpenAI-compatible model with independent grading,
durable run history, cancellation and checksummed exports. Workspace candidate code
runs in isolated Docker containers; answer profiles supply no code tools or candidate
container. These small, published tasks are not a broad model ranking and do not
measure resistance to benchmark memorization.

The **provider library** adds explicit source imports and isolated native execution
for upstream coding, terminal, browser, security and refusal suites. It also
contains nine original frontend-design briefs across two suites, installed only
after their bundled sources are explicitly fetched and prepared for a local Chromium runtime.
The unfinished application security labs are archived and unavailable for
new preparation or runs. Published, imported and prepared counts are separate. Start with
`benchmaxxing providers list`; see [provider preparation](docs/providers.md) for
the 250 GB cache cap, full-source downloads, runtimes and offline constraints.

| Category | Tasks | Examples |
| --- | ---: | --- |
| Programming | 27 | Matching, connectivity timelines, knapsack ties, exact money allocation, Unicode offsets |
| Numerical engineering | 20 | Quaternion rotation, mesh metrics, inertia tensors, AC circuits, thermal systems, numerical methods |
| Defensive security | 15 | DNS/CIDR policies, dependency audits, audit ledgers, permissions, URL origins, credential expiry, container isolation policy |
| Terminal | 12 | SQLite reports, checksum audits, deployment diffs, CI summaries, config parsing, log triage |
| Refusal/policy decisions | 8 | Synthetic secret protection, permitted requests, log/document injection, export consent, lab scope |
| Offline knowledge | 8 | Original factual questions in science, history and geography; 64 checks |
| Context retrieval | 36 | Seeded 32/128/512 KiB contexts, absent needles and distractors; 216 checks |

Browse and search the catalog in the UI, choose a task or filtered batch, and
repeat each task up to five times. One controller executes up to eight attempts
in parallel, with a saved concurrency limit that defaults to one.
Coverage shows distinct graded tasks and attempt outcomes within loaded history;
it is not an overall model-quality score. See [task pack authoring](docs/task-packs.md).

The live session shows returned model text, file edits, tool arguments and command
output as they arrive, directly below run progress. Follow the active run or pin
another inspection; pause the feed to read without losing your place. Raw sequenced
events and complete exports remain available. Progress polling uses the task-specific
`/api/v1/run-progress` route to avoid generic activity-tracker filters.

New runs default to a 30-minute wall-clock budget per task, including the bundled
Qwen profile. Model-turn, response-token and command limits remain separate.
Time and turn consumption do not imply percent complete or an ETA. See
[live monitoring](docs/task-packs.md#live-monitoring) for details.

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

When updating an already prepared checkout, stop the service and register the
installed catalog against the existing local image without downloading:

```sh
benchmaxxing prepare --no-pull
```

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

Configure an existing server that supports chat completions. Workspace tasks also
require tool calls; answer profiles request a single JSON response without tools.
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
| `tasks --category engineering` | List task IDs, descriptions and grading rubrics; no Docker needed. |
| `validate-packs` | Validate installed pack schemas, asset paths and grading definitions. |
| `suite --category programming --repeat 2` | Queue independent attempts; repeat `--config` to test several models and set `--parallel 2` to run them concurrently. |
| `serve` | Start the loopback-only UI and API on port 8123. |
| `export RUN_ID -o result.zip` | Export a finished run with traces, artifacts and checksums. Existing files are never overwritten. |

`run` exits 0 for a passed task, 1 for task failure, and 2 for infrastructure,
unsupported capability or budget/cancellation outcomes. Ctrl+C requests cancellation.
Each repeated run creates a new ID. Interrupted runs retain evidence and become
`interrupted` when their controller restarts.

The web UI can save multiple OpenAI-compatible endpoints in the selected local
data directory. Add an endpoint, choose Chat Completions or Responses, and discover
its `/v1/models` list. Check the models to run; the composer queues one attempt per
checked model, selected task, and repeat. An endpoint can also use a manually
entered model ID when discovery is unavailable. Only an API key environment
variable name is stored; the key remains in the controller's environment. Offline
endpoints are restricted to loopback. Discovery does not use inherited proxies or
follow redirects. The Responses adapter currently reads complete JSON responses
before emitting their text and tool events; Chat Completions supports live SSE.
Encrypted reasoning replay is optional for Responses servers that support
`reasoning.encrypted_content`; leave its checkbox off for servers that do not.

Set **Max parallel runs** in the composer to test models from different endpoints,
or several models on the same endpoint, concurrently. The saved limit (1–8)
applies to the controller's entire queue, including CLI submissions. Reducing it
lets running attempts finish before starting more. Each attempt has independent
budgets, cancellation, artifacts and grading; concurrent attempts share host and
inference-server resources. The live monitor lists every running attempt.

For vLLM, set the optional **vLLM priority** on a saved endpoint or custom model
profile, or add `vllm_priority = -1` under `[inference]` in a run configuration.
Lower integers have higher priority; zero is an explicit priority. Leave it blank
for other servers: the `priority` request field is omitted when unset. Nonzero
priorities require vLLM to run with `--scheduling-policy priority`. This controls
inference-server scheduling, independently of the benchmark concurrency limit.

For mixed-category batches, add the current task or the filtered task set to
**My task selection**, change catalog filters, and add more. The composer shows
the resulting task × model × repeat count before queueing. Each attempt keeps
its own 30-minute default wall budget and evidence.
The composer also sets the max output tokens per model turn (1–32,768, default
16,384); this value is recorded in every run specification and export.

The comparison panel graphs category pass rates for two model profiles over
matching task versions, hashes, harnesses, graders, generation settings, budgets,
API styles, images, and network conditions. It shows coverage and excluded attempts; it does
not combine unlike tasks into an overall score.
Matching also accounts for request priority and the recorded concurrency limit.
Time to completion is recorded as `metadata.elapsed_seconds` in each run and
export. The history and comparison graphs show it separately from correctness.
Timing medians include only passed attempts with recorded elapsed time; queue
wait and failed attempts are excluded from those medians.

```sh
benchmaxxing run --task python.shortest-route --config examples/strata-qwen.toml
benchmaxxing suite --category engineering --config examples/strata-qwen.toml
benchmaxxing suite --task terminal.log-triage --task security.acl-specificity --repeat 3
benchmaxxing suite --category programming --config examples/local-model.toml --config examples/api-model.toml --parallel 2
benchmaxxing run --config examples/local-model.toml --vllm-priority -1
```

`suite` with no category/task selection runs the entire installed catalog. With no
model configuration it uses the scripted good fixture; `--variant bad` verifies
rejection. Scripted outcomes are infrastructure checks, not model performance.

Exports contain `run.json`, `events.jsonl`, `artifacts.json`, artifact bytes, and a
SHA-256 manifest. The SQLite database remains the authority for live state and
events. Model/tool text can contain sensitive task data; exports are local files
and are not automatically published.

Workspace pack runs also snapshot the original public task assets before execution,
so terminal input files remain available after an installed pack changes. Answer
profiles preserve the exact prompt and supplied context as checksummed artifacts.
Private grading cases and reference solutions never enter the candidate workspace.

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
isolation and recovery. The [roadmap](docs/roadmap.md) prioritizes software,
isolated cybersecurity labs, frontend UI design, offline world-knowledge retention
and needle-in-haystack retrieval, while retaining numerical engineering.

The 126-task original core catalog, nine original frontend briefs, parallel repeated
trials, live activity monitoring and category coverage are implemented. Knowledge
and retrieval profiles have deterministic exact-answer grading; frontend briefs
have functional browser checks and separate unscored visual review artifacts.
The numerical geometry tasks use JSON inputs and outputs. Application security labs remain archived pending a complete
isolation and runtime review. External agents, broader task packs, distributed workers
and richer comparisons remain future milestones. There is no shared hosting,
distributed worker farm, managed inference runtime or universal leaderboard.

Project code is **AGPL-3.0-only**. See [LICENSE](LICENSE). Imported tasks, datasets
and tools retain their own licenses.
