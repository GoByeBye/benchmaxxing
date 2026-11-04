# Provider release validation

Validated locally on 4 November 2026. This records the upstream-provider release;
[foundation validation](validation.md) preserves the earlier original-pack results.
These are software and adapter checks, not model-quality leaderboard results.

## Final checks

| Check | Observed result |
| --- | --- |
| Windows core suite | 316 passed; 100 Docker checks deliberately deselected |
| Linux core suite | 316 passed; 100 Docker checks deliberately deselected; controller ran with `--network none` |
| Final progress, jobs, accounting and source follow-up | 26 passed on Windows and 26 on offline Linux after the final preparation-progress and job-lifecycle changes |
| Chromium UI suite | 32 passed |
| Firefox UI suite | 32 passed |
| Final provider UI follow-up | Chromium 8 passed; Firefox 6 passed initially, then both timed-out cases passed on an unchanged-source rerun |
| TypeScript and production UI build | Passed |
| Generated API contracts | OpenAPI matches the Python API; generated TypeScript matches the recorded schema, including SWE preparation endpoints |
| Ruff lint and formatting | Passed; 84 Python files formatted |
| Strict mypy | Passed for 43 source modules |
| Real MiniWoB browser checks | Three passed: native positive/negative reward and timeout cleanup |
| Real Harbor grading fixtures | Eight passed: good/bad submissions for three reviewed Terminal-Bench 2.1 tasks and one reviewed Terminal-Bench 4 task |
| Native interruption recovery | Three real Docker checks passed: cancellation, command timeout and symlink rejection |
| Offline wheel and source build | Passed using the prepared build cache; archive checks verified all provider/runtime/UI resources and excluded private state |
| Fresh installed-wheel smoke | Locked, hash-checked dependencies installed offline from the prepared wheelhouse; native good/bad runs and export checks passed |
| Real browser against installed wheel | One integration scenario passed, creating correct and incorrect reference runs and inspecting grading, artifacts and export |

The final core suites used Python 3.13. The Linux controller image was
`sha256:9bdc0121c0a414d7a1b551dc3792e2418e9f0460242cd5ac9498664b483b9a6d`.
Its source, tests, examples and configuration were mounted read-only; it received
no Docker socket. Docker-dependent checks above ran separately against explicitly
prepared images. Candidate programs ran only in disposable Linux containers.
The Windows host remained connected; this is not a claim of host-wide egress isolation.
Both core suites reported the same upstream Starlette TestClient deprecation warning.

The 100 deselected Docker cases were not all rerun for this release. Earlier
original-pack fixture validation is documented separately. A passing provider
adapter check does not mean that every imported task has been executed.
The full core and UI suites preceded the final preparation-progress changes;
the targeted follow-ups cover those changes. The final backend regressions also
verify that polling cannot overwrite a completed job with an interrupted state,
failed jobs keep their last progress, preparation acceptance defers catalog reads
to its worker, and suite filtering skips unrelated records before reading them.
These test counts overlap and should not be added together as a count of unique checks.

The final Firefox follow-up encountered intermittent Playwright timeouts: one while
closing the browser context after all assertions had passed, and another while
scrolling a visible element into view. The two cases passed with tracing disabled;
the tests and application were unchanged. This is not presented as a clean final
eight-case Firefox invocation, and the original timeout logs were retained locally.

## Verified interactive paths

The complete frozen MiniWoB++ HTML source imported 125 registered BrowserGym
templates, and all 125 received image/dependency/asset preparation records.
The real positive and negative browser trials used `miniwob.click-test`.
They verified native rewards of one and zero, accessibility observations,
screenshots, restricted navigation and owned-container cleanup. This was not a
125-task model run.

Terminal-Bench sources imported 89 upstream tasks from 2.1 and 66 from 4.0.0.
The reviewed offline adaptations tested here were:

| Native task | Preserved submission | Observed outcome |
| --- | --- | --- |
| `terminal-bench/cancel-async-tasks` | `/app/run.py` | Good reward 1; incorrect reward 0 |
| `terminal-bench/sound-change-cascade` | `/app/rules.json`, `/app/ordering.txt` | Good passed all seven upstream assertions; incorrect failed four and received reward 0 |
| `terminal-bench/regex-log` | `/app/regex.txt` | Upstream solution reward 1; `.*` reward 0 |
| `terminal-bench/sqlite-db-truncate` | `/app/recover.json` | Upstream solution reward 1; empty JSON array reward 0 |

All four use a fresh immutable grader image. Native test inputs and assertions remain
unchanged, while grader dependencies are installed during explicit preparation.
The sound-change image omits the optional upstream Git executable and uses the
distinct `benchmaxxing-offline-30m-v1-artifacts-v1-nogit-v1` variant. Its build added
36,081,678 image bytes to an already present pinned base, using six hash-locked
wheels totaling 1,808,670 compressed bytes. The build itself used no network.
The `regex-log` and `sqlite-db-truncate` adaptations use distinct
`benchmaxxing-offline-30m-v1-artifacts-v1-prepacked-pytest-v1` variants. They
replace the upstream verifier's task-time dependency bootstrap with the already
prepared Python 3.13 image containing pytest 8.4.1 and pytest-json-ctrf 0.3.5;
the upstream `tests/test_outputs.py` files and assertions remain unchanged.
For `regex-log`, this changes the task runtime from upstream Ubuntu 24.04 to
Python 3.13 slim Bookworm, so the variant must be compared on its own conditions.
`regex-log` reused local image
`sha256:b82003632ab682d7c3fef1f7eb74d014fd7657564629c3d49427b1d6e6b2330b`.
`sqlite-db-truncate` built local image
`sha256:c0499af9ff3681f61f27439585c5a7d8bfb5721b7ebb52ee5d8e676f87084156`
from that present base with `--pull=false --network none`; its immutable input
`/app/trunc.db` matched SHA-256
`a7f00ee232fe621629dc014eb2619ac793131843ede1fd892f3b8b7e17332a5b`.
The good/bad fixtures ran in disposable candidate and separate grader containers.
Neither candidate had the private verifier files, and both used Docker `--network none`.
These are two additional prepared tasks, not a full Terminal-Bench suite claim.
Other Harbor tasks require separately reviewed environments and artifact roots;
whole-system snapshots and candidate-image reuse are unsupported.

Complete WebArena-Verified (812 tasks) and VisualWebArena (910 tasks) definitions
were imported. Their website environments are not marked prepared. The WebArena
inventory measured 187,334,164,480 bytes for the three map archives; upstream
documentation estimates a further 100 GB for Wikipedia. That is about 287.33 GB
before image and extraction overhead, above this installation's 250 GB cap.
The original Wikipedia endpoint was unavailable, and registry rate limiting
prevented an exact website-image inventory, so those sizes and hashes remain
unknown. No partial website deployment was presented as a complete benchmark.
See the pinned upstream [map prerequisites](https://github.com/ServiceNow/webarena-verified/blob/6473f72db5dcefc97b5725b59e734504edc28a21/docs/environments/map.md)
and [Wikipedia prerequisites](https://github.com/ServiceNow/webarena-verified/blob/6473f72db5dcefc97b5725b59e734504edc28a21/docs/environments/wikipedia.md).

## Evidence, recovery and limits

Imported records, prepared image counts and published suite sizes remain separate.
Different local variants retain separate immutable records, so a development data
directory can contain more records than the upstream task count. Consult the
provider panel or `benchmaxxing providers list` for the selected directory's actual
readiness. Full source imports do not imply full runtime coverage.

Cancellation and timeouts preserve bounded partial native outputs by stopping the
owned container and reading regular files from Docker's TAR stream. Recovery does
not execute a candidate materialization command, extract files onto the host, or
resume the candidate. It verifies workspace/run labels, rejects links, devices,
traversal and ambiguous entries, and records missing or over-budget artifacts.
Recovery is limited to 16 MiB, 4096 archive entries and 20 seconds. Direct roots
such as `/workspace/solution.py` and `/app/run.py` are supported; deeper roots are
reported unavailable because stopped-container ancestor symlinks cannot be
verified through this interface. Recovered partial evidence is not marked submitted
and never converts cancellation or exhaustion into a pass.

Storage checks cover the configured source cache, asset copies and distinct
recorded provider images, with a separate physical Docker-disk reserve. Preparation
operations are serialized across API and CLI entry points. The API and CLI enforce
the 250 GB maximum. Repointing a cache with installed assets requires an explicit
offline migration instead of silently invalidating prepared runs.

Native graders retain their upstream trust assumptions; they may execute candidate
code during evaluation. They are a different track from the original packs'
controller-side black-box comparisons. Continuous scores do not invent pass
thresholds, and human/judge-dependent results remain ungraded. Hosted services,
unprepared local judges, missing GPU runtimes and unavailable private assets remain
explicit blockers. No model weights or drivers are installed by execution.

The primary local Qwen queue was left running throughout these release checks.
Mock models and deterministic fixtures were used for the checks above; they are
not additional Qwen benchmark results. macOS support and full-suite model-quality
validation remain outside this release verification.

## Reproduction and distribution checks

```sh
uv run --locked --offline pytest -m "not docker" -q
uv run --locked --offline ruff check .
uv run --locked --offline ruff format --check .
uv run --locked --offline mypy
```

Use the already installed Node dependencies and prepared Playwright browser
binaries for `npm run build`, `npm test` and `npm run test:firefox`. API generation
must leave `web/openapi.json` and `web/src/api-types.ts` consistent. These commands
do not prepare missing packages or browser binaries automatically.

`scripts/check_dist.py` checks wheel and source archives for private/runtime state,
all original packs, the built UI, provider modules, optional runtime scripts,
dependency locks and provider documentation. Dependency images, benchmark datasets,
credentials, run databases and model weights remain outside the distributions.

The installed-wheel smoke ran from a new isolated environment, verified that imports
came from `site-packages`, and served bundled assets without Node in the application
process. It prepared only already present images, imported an explicitly labeled
test fixture, and checked native pass/fail outcomes through the CLI attached to its
separate localhost service. API and CLI records agreed, and every exported file's
size and SHA-256 matched the bundle manifest. The real browser then created two
original-reference attempts. This used a separate data directory and ephemeral port;
the primary service was not restarted and its run data was not altered by these checks.
