# Upstream benchmark providers

The provider library separates published suite sizes, imported task records and
prepared runtimes. A published count is never added to runnable coverage. The
original 81 tasks remain installed without fetching third-party materials.

All imported attempts use `benchmaxxing-imports/1`. Their identity includes the
provider, original task ID, source revision, source SHA-256 and local variant.
The default `benchmaxxing-offline-30m-v1` variant has a 30-minute wall budget;
upstream leaderboards may use different networks, tools, time limits or graders.
Do not combine these results with official leaderboard scores without matching
the evaluation conditions.

## Explicit preparation

The UI's provider library and the following commands share the selected data
directory. `fetch` is an explicit network operation: it downloads a complete
pinned source, checks its integrity, converts data, and installs task records.
Parquet parsing and source-code materialization run in isolated preparation
containers. Source Python and shell scripts are never executed on the host.

```powershell
benchmaxxing providers configure --cache-dir D:\BenchmaxxingCache --limit-gb 250
benchmaxxing providers list
benchmaxxing providers fetch evalplus-humaneval
benchmaxxing providers prepare evalplus-humaneval
benchmaxxing tasks
```

Linux operators can use an absolute cache path on their chosen data disk. The
default cap is **250,000,000,000 bytes total**, with a 10 GB free-space reserve.
Source archives, expanded sources, copied assets and recorded imported images
count toward that cap. Shared asset blobs and image identities are counted once.
Runtime preparation also checks physical Docker storage where discoverable;
moving the source cache does not move Docker Desktop's virtual disk.

For an existing local source, use an exact upstream revision:

```sh
benchmaxxing providers import terminal-bench-2-1 /data/terminal-bench-snapshot --revision COMMIT_SHA
benchmaxxing providers prepare terminal-bench-2-1 --image YOUR_PREPARED_IMAGE
benchmaxxing run --task imported.PROVIDER-TASK-ID --config examples/strata-qwen.toml
```

Omitting `--image` explicitly builds a reviewed provider runtime, or verifies
existing pins when the complete suite is already prepared. This build may download
dependencies. Supplying `--image` selects an existing local image instead; that
path never pulls unless `--pull` is explicit and the task declares a bounded
image-size ceiling. Running
an imported task only verifies existing assets and image pins; it never repairs,
installs or downloads missing dependencies. Failed preparation reports the exact
task and missing requirement. Existing completed runs retain their evidence.

The HTTP API exposes `/api/v1/providers`, `/providers/settings`, `/providers/jobs`,
and mutation endpoints `/providers/fetch`, `/providers/import`, `/providers/prepare`.
Mutation endpoints retain the session-token and origin checks. Long preparation
jobs persist their status; unfinished jobs become interrupted after a service
restart. Repeating explicit preparation can reuse verified cached sources.

SWE-bench uses separate instance images. `providers swe-plan TASK_ID` writes the
exact dependency script, Dockerfile and offline evaluation adaptation for review.
After inspecting the upstream image's storage requirements, use
`providers swe-prepare TASK_ID --reserve-gb N --pull` for an explicit bounded build.
The parser and tests retain their upstream identities. Unsupported dependency
setup is rejected rather than silently left in the offline evaluation phase.

## Provider families

| Provider | Evaluation path | Additional requirements |
| --- | --- | --- |
| Terminal-Bench 2.1 / 4.0 | Harbor task import and native rewards | Per-task images, offline verifier dependencies and explicit bounded artifact roots; system-state tasks require a separate adapter |
| MiniWoB++ | BrowserGym browser observations/actions and native reward | Frozen HTML, pinned Chromium and BrowserGym image |
| WebArena-Verified | Native browser task records and local website state | Complete website snapshots, fresh task-local services and verified reset |
| VisualWebArena | Visual browser observations and native task definitions | Website services, vision model, prepared local grading models |
| HumanEval+ / MBPP+ | Frozen expanded assertions or official differential input format | Prepared Python/NumPy runtime; source format is recorded |
| BigCodeBench | Original task tests and pinned native harness | Official library environment and harness revision |
| SWE-bench Lite / Verified | Original issue, repository revision, patch and native test/log grading | Per-instance images and pinned evaluation script/parser |
| SciCode-Verified | Frozen verified targets and scientific Python checks | HDF5 targets and declared whole-problem evaluation variant |
| InterCode-CTF | Native flag checks with private expected values | Prepared challenge files/tools; external services remain explicit blockers |
| Cybench | Challenge records, local services and native final-answer grading | Controlled service graphs and adapted unprivileged setup |
| CyberGym | Vulnerability/reproducer task definitions | Full assets and evaluation server; complete preparation exceeds this installation's cap |
| XSTest | Response capture with separate refusal rubric | Human assessment or a separately configured local judge |
| HarmBench | Text/multimodal behavior records and response capture | Local classifier for graded results; image-capable participant for multimodal tasks |
| WorkArena | Cataloged upstream integration boundary | Hosted ServiceNow required; fully offline execution unavailable |

The installed provider panel reports the current machine's actual readiness.
Fetching a suite does not imply that every runtime, website, GPU or judge is ready.
No model weights are installed automatically. CyberGym's complete published
source/binary assets alone exceed the selected cap; its full image collection is
larger still. Hosted-only providers remain visible with their concrete blockers.

## Execution, grading and evidence

Native tools stream through the same sequenced event store as original tasks.
Browser observations include accessibility data and screenshots; actions use
observed identifiers. Browser rewards and private evaluation state stay outside
participant observations. Browser screenshots and submissions become recorded
artifacts. Tool, model-turn, output, memory and wall-clock budgets remain separate.

Candidate containers receive no host mounts, Docker socket, controller credentials
or grading bundle. Imported native runtimes use unprivileged writable container
layers with a monitored size ceiling. The candidate is stopped, bounded declared
artifacts are captured, and native grading runs in a fresh immutable base image.
Candidate-modified images are never committed or reused as grading runtimes.
Browser service graphs use fresh owned containers on internal networks, with no
published host ports or shared website resets.

Native upstream graders may execute submitted code while evaluating it. Their
trust assumptions differ from the original packs' controller-side black-box
comparison, and native results are identified as a separate track. These adapters
do not claim to make every upstream grader resistant to adversarial grader attacks.

Binary outcomes, continuous scores and ungraded responses are distinct. A
continuous native score has `passed: null`; an unassessed response has no scorecard
and finishes as `completed` with `assessment_status: needs_review`. Neither is
counted as a pass. Refusal is recorded separately from correctness; no keyword
heuristic silently supplies an official HarmBench/XSTest judgment.

Exports retain the original IDs, source hashes, image identity, local variant,
harness version, budgets, observations, submission artifacts and native grading
evidence. Imported materials keep their upstream license notices. They are stored
outside Git and are not relicensed as project code.
