# Original task packs

The installed catalog contains 82 tasks and 681 grading checks: 27 programming,
20 numerical engineering, 15 defensive security, 12 terminal and 8 synthetic policy
decision tasks. The original interval-merging task retains its existing identity
and public-asset hash. The current harness identity is `benchmaxxing-tools/3`.

| Category | Tasks | Example task IDs |
| --- | ---: | --- |
| Programming | 27 | `python.bipartite-matching`, `python.money-allocation`, `python.unicode-offsets` |
| Numerical engineering | 20 | `engineering.quaternion-point-rotation`, `engineering.triangle-mesh-metrics`, `engineering.stable-real-quadratic` |
| Defensive security | 15 | `security.ordered-cidr-firewall`, `security.dependency-version-audit`, `security.credential-expiry-events` |
| Terminal | 12 | `terminal.sqlite-sales-report`, `terminal.checksum-audit`, `terminal.deployment-diff` |
| Refusal/policy decisions | 8 | `refusal.require-export-consent`, `refusal.respect-lab-scope`, `refusal.ignore-log-instructions` |

## Running the catalog

```sh
benchmaxxing tasks
benchmaxxing tasks --category security
benchmaxxing validate-packs
benchmaxxing prepare --no-pull
benchmaxxing run --task terminal.release-manifest
benchmaxxing suite --category engineering --repeat 2 --config examples/strata-qwen.toml
```

Stop the local service before preparation. Omit `--no-pull` only when explicitly
preparing an image from a registry. Neither execution nor validation installs
dependencies. A suite submits individual run records through the shared controller.
Set `--parallel 2` to execute two attempts concurrently (1–8, default one), and
repeat `--config` to test several models on each task. The concurrency limit is
saved for the data directory and applies to CLI and UI submissions. Each trial
gets its own trace, result, artifacts and export. Ctrl+C
cancels queued suite attempts and requests cancellation of active work.

The UI searches task titles, IDs, tags and descriptions; filters by category;
shows instructions and rubrics; and can queue the filtered catalog with 1â€“5
attempts per task. Category coverage is based on loaded history, limited to the
latest 1,000 attempts, and distinguishes graded task coverage from passed attempts.
It is not a comparable aggregate across different participants or settings.

## Live monitoring

The UI requests `GET /api/v1/run-progress` on an approximately one-second cadence.
This compact snapshot describes queued and running attempts using a durable
SQLite progress projection updated in the same transaction as each source event.
It includes queue positions, phase, model turn, last tool, last activity time,
cancellation state and completed/total grading checks. It does not replay full
prompts or traces to calculate the activity panel.

The activity panel lists all running attempts when parallel execution is enabled.
Enable **Follow active run** to move inspection to the current active attempt.
Selecting a run from history or the activity panel pins that inspection; enable
follow again to resume automatic selection. The activity panel continues updating
while inspection is pinned and identifies delayed or reconnecting snapshots.
Elapsed time and model turns show budget consumption, not a completion percentage
or an ETA. Grading progress counts checks actually reported by the grader.

The session conversation appears above the catalog. It shows incremental model
text and provider-returned reasoning, file reads/writes, pending tool arguments and
live stdout/stderr. Completed response/tool records replace provisional content
without duplication. The session keeps a separate bounded collection of recent
turns, so raw-trace window eviction does not erase an ongoing response. Long fields
are shortened visibly; full evidence remains in the trace/export. Pause or scroll
up to freeze the reading view, then choose **Jump to live session** to resume.

New task runs default to 1800 seconds. Changing defaults never edits a recorded
attempt's configuration; reruns use new IDs. Response-token, turn and command
limits still apply separately. The deprecated `/api/v1/activity` alias remains
available for older clients; the task-specific path avoids generic tracking filters.

The selected run's trace uses server-sent events at
`/api/v1/runs/{run_id}/events?after={sequence}`. The UI batches incoming events
before rendering, ignores duplicate sequence numbers and reconnects after the last
received sequence. It displays up to 300 events. Scrolling up or opening older
evidence freezes that reading window while new events accumulate in a separate
bounded window; the complete stored trace remains available in the export.
**Jump to latest** resumes following the newest events. Results and artifacts refresh
alongside the trace, so completed evidence remains inspectable after live work ends.

## Pack contract

Add a JSON file under `src/benchmaxxing/packs/<category>/`. Restart the controller
after changing installed packs, validate, and prepare again. The authoritative
schema is `TaskDefinition` in `src/benchmaxxing/task.py`; all unknown fields are
rejected. Each definition includes:

- A stable `id`, semantic `version`, title, category, difficulty and tags.
- A precise prompt with inputs, outputs, tie breaking, units and error behavior.
- Public UTF-8 workspace `files`, including `public_tests.py`; TASK.md is generated.
- `kind: python` with a starter solution.py and named single-argument entrypoint,
  or `kind: json` with a submission.json artifact contract.
- Good and deliberately incorrect reference submissions retained by the controller.
- Named cases with literal expected values. Python cases supply one JSON input;
  artifact cases use a path of object keys/list indices into the submitted JSON.
- Explicit absolute/relative numeric tolerances and input-preservation requirements.

Only the prompt and public files enter the candidate container. The HTTP catalog
does not expose private cases or fixtures. Python submissions run in a fresh
grading container; comparisons happen outside it. Booleans are not treated as
numbers, JSON object keys and list ordering matter, and NaN/Infinity are rejected.
Terminal artifacts are compared as data. The reference fixtures never execute on
the host. All authored code currently uses the Python standard library.

The task hash covers the complete canonical definition, so changing a check or
reference fixture invalidates preparation. Increment the task version when
changing its benchmark conditions. Grader identities include task/version.
Exports include the original public asset snapshot and submitted artifact; use
the matching installed pack and grader version for re-evaluation.

## Refusal and security scope

Refusal packs ask for a structured decision against a written synthetic policy.
They cover both refusal and permitted compliance, including document injection.
They require exactly the declared fields and cannot hide extra content in ignored
keys. Expected/observed decisions are stored in `metadata.refusal_assessment`;
the run's provider-refusal field and correctness score remain distinct. These
tasks do not claim to measure nuanced natural-language refusal quality.

Security tasks cover defensive parsing, normalization, policy and state analysis.
Engineering tasks assess specified simplified formulas and numerical behavior,
including quaternion transforms, indexed mesh geometry and point-mass inertia.
These are JSON function tasks rather than certifications for real-world engineering
design. The container-isolation policy task audits synthetic JSON configuration only; it does
not launch containers or establish that a real deployment is isolated. Interactive
browser apps use a separate environment and grader. Application security labs remain
archived and unavailable for new preparation or execution.

## Required validation

`validate-packs` checks definitions and paths but does not establish correct
expectations. Run all fixtures in Docker before accepting a pack:

```sh
# Set BENCHMAXXING_DOCKER_TESTS=1 and BENCHMAXXING_TEST_DATA_DIR to prepared data.
pytest tests/integration/test_packs_docker.py -q
```

Every pack must pass its public examples and all independent checks with the good
fixture, and fail at least one check with the bad fixture. Independently review
literal expected answers; never generate them by blindly invoking the candidate
reference solution. Keep tasks original and preserve the AGPL-3.0-only license.
