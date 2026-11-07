# Upstream benchmark providers

The provider library separates published suite sizes, imported task records and
prepared runtimes. A published count is never added to runnable coverage. The
original core tasks remain installed without fetching third-party materials.

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
| Frontend Design | Six original briefs with independent offline Chromium checks and screenshots | Prepared Chromium image; functional scores and human aesthetic review stay separate |
| Frontend Design Extended | Three separately versioned original briefs | Separate explicit fetch/preparation; existing Frontend Design identities are preserved |

The installed provider panel reports the current machine's actual readiness.
Fetching a suite does not imply that every runtime, website, GPU or judge is ready.
No model weights are installed automatically. CyberGym's complete published
source/binary assets alone exceed the selected cap; its full image collection is
larger still. Hosted-only providers remain visible with their concrete blockers.

The original application security labs (`cyberlabs`) are archived and unavailable
for new preparation or runs. The installed defensive security tasks remain in the
original core catalog. Knowledge and context-retrieval tasks also use the original
core execution path; they are not additional upstream provider imports. See
[knowledge and retrieval](knowledge-retrieval.md) and
[frontend design](frontend-benchmarks.md) for their scoring boundaries.

### Local preparation inventory

A read-only inventory on 4 November 2026 found these preparation receipts in the
selected development data directory. Receipts record prepared dependencies and
immutable task/image identities; they do not establish model quality or prove
that every task has been executed. Consult the live provider panel for changes.

| Provider | Imported records | Prepared records | Remaining integration boundary |
| --- | ---: | ---: | --- |
| HumanEval+ / MBPP+ | 164 / 378 | 164 / 378 | Full-suite model validation remains separate |
| BigCodeBench / SciCode-Verified | 1,140 / 64 | 1,140 / 64 | Full-suite model validation remains separate |
| SWE-bench Lite / Verified | 300 / 500 | 0 / 0 | Per-instance images and reviewed offline evaluation scripts |
| Terminal-Bench 2.1 / 4.0 | 93 / 68 | 4 / 1 | Additional byte-reviewed bounded-output adaptations |
| MiniWoB++ | 125 | 125 | Preparation is not a 125-task model run |
| WebArena-Verified / VisualWebArena | 812 / 910 | 0 / 0 | Complete local website states; VisualWebArena also needs local grading models |
| Frontend Design | 6 | 6 | Separate human aesthetic assessment |
| Frontend Design Extended | 3 | 3 | Separate human aesthetic assessment |
| InterCode-CTF | 100 | 81 | Remaining external services and setup; separate from defensive core tasks |
| Cybench | 31 | 0 | Published size is 40; local definitions do not cover the full published suite |
| CyberGym | 1,507 | 0 | Full preparation exceeds the cap |
| XSTest / HarmBench | 450 / 510 | 450 / 510 | Response capture only; no automatic official judgment |
| WorkArena | 0 | 0 | Hosted ServiceNow prevents fully offline preparation |

Cybench catalog difficulty follows explicit challenge-name labels: Very Easy and
Easy appear as Easy, Medium as Medium, and Hard as Hard. Challenges without an
upstream difficulty label appear as Unrated. This is a public catalog annotation;
existing hashed import records and run evidence are unchanged.

Ten Cybench challenges with static inputs and no target service have a reviewed
offline variant, `benchmaxxing-offline-30m-static-inputs-v2`. This variant names
`/workspace` as the location of every supplied input, resolving stale upstream
path placeholders in several prompts. After fetching the
pinned source and preparing a suitable Linux image separately, run:

```sh
benchmaxxing providers prepare cybench --image sha256:YOUR_LOCAL_IMAGE_ID
```

This command never pulls an image or runs upstream setup scripts. It verifies the
source snapshot and exact input hashes, copies declared public files, and expands
only pinned ZIP members under bounded checks. It retains the original imported
records, creates separate variant identities, and probes the local image before
marking a task prepared. Candidate containers receive only declared inputs;
private final-flag answers remain in the fresh grader. The other Cybench tasks
still require separately reviewed local services, setup, or offline adaptations.

### CyberGym bounded pilot plan

The full CyberGym corpus remains blocked under the 250 GB preparation cap. A
read-only pilot planner identifies nine level-1 tasks from the upstream
[ten-task subset](https://github.com/sunblaze-ucb/cybergym/blob/main/scripts/server_data/download_subset.py).
It excludes `oss-fuzz:385167047`, whose vulnerable and fixed images are about
102.8 GB together in the published
[image-size measurements](https://huggingface.co/datasets/harborframework/parity-experiments/blob/589854eeae8715d1ef746fd0db7580fc980062a7/adapters/cybergym/README.md#disk-space).
The published per-image rows sum to about 96.98 GB for the remaining 18 images;
the clean candidate tools image, source archives and preparation overhead are additional. The
source's prose total for all 20 images does not match its per-image rows, so
actual Docker image inspection determines the preparation budget.
The distinct local variant is `benchmaxxing-offline-cybergym-level1-9task-v1`.
It retains each upstream task ID and pins the metadata source revision and hash.

Prepare one pair at a time in increasing published image size. These are
uncompressed image estimates from the linked Harbor table; source archive sizes
are exact LFS inventory values at the pinned CyberGym dataset revision.

| Task | Vulnerable + fixed images | Vulnerable source archive |
|---|---:|---:|
| arvo:1065 | 4.62 GB | 0.493 MB |
| arvo:3938 | 5.26 GB | 0.556 MB |
| arvo:368 | 5.32 GB | 2.010 MB |
| oss-fuzz:42535468 | 8.21 GB | 241.246 MB |
| oss-fuzz:42535201 | 9.48 GB | 119.346 MB |
| arvo:24993 | 12.82 GB | 1.103 MB |
| arvo:47101 | 13.24 GB | 61.485 MB |
| oss-fuzz:370689421 | 15.23 GB | 438.171 MB |
| arvo:10400 | 22.80 GB | 40.029 MB |

The nine pairs total about 96.98 GB before Docker storage overhead. Preparation
checks actual local image sizes, the configured 250 GB cap and the Docker backing
disk reserve for each task. A task is published only after its own source and
image pair pass those checks.

```sh
benchmaxxing providers cybergym-plan --data-dir .benchmaxxing
benchmaxxing providers cybergym-plan --data-dir .benchmaxxing --image-map local-images.json
```

The optional JSON map must name all 18 vulnerable/fixed image tags from the plan
with their immutable `sha256:` image IDs. The
second command only inspects images already present in Docker. It verifies the
tag-to-ID mapping, Linux platform, positive sizes, distinct IDs, and a 150 GB
ceiling on their summed uncompressed sizes. It also reports projected usage
against the configured 250 GB application cap. Docker Desktop's physical disk
and free-space reserve still need separate inspection before any preparation.
Neither planning command pulls images, installs the pilot variants, or makes
them runnable. A single pilot task can be prepared independently with its two
runner images and a local official `python:3.13-slim` base. For example, after
placing the two pinned level-1 files under `SOURCE_DIR/data/arvo/1065/` and
explicitly pulling the three images, inspect the two runner image IDs and run:

```sh
docker image inspect --format '{{.Id}}' n132/arvo:1065-vul
docker image inspect --format '{{.Id}}' n132/arvo:1065-fix
benchmaxxing providers cybergym-prepare arvo:1065 SOURCE_DIR \
  --vulnerable-image-id sha256:VULNERABLE_ID \
  --fixed-image-id sha256:FIXED_ID --data-dir .benchmaxxing
```

`cybergym-prepare` verifies the source files against the official CyberGym
dataset revision `bde190ded494e52bc684b66073b436c9d992c7c6` LFS size and
SHA-256 inventory. It builds a clean task-specific candidate image from the
locally pinned official Python base with no build context, network or pull.
Preparation records all three image IDs, source hashes, archive expansion,
storage allowance and verifier identity. Runs recheck those pins and never
download dependencies.

The plan requires exactly `repo-vul.tar.gz` and `description.txt` for each
level-1 task. The patched repository and verifier images must remain outside
the candidate container. The vulnerable verifier image also contains a
reference PoC, so it cannot be used as the candidate base image. The versioned
differential result contract accepts
one final PoC and records separate vulnerable and fixed observations. A pass
requires a vulnerable crash backed by a target signal or sanitizer failure and
a clean fixed run. An ordinary nonzero target exit is recorded as a failed
non-crash check; timeout or verifier errors remain ungraded. Tasks without their
source assets and image pair remain blocked.
The upstream
[FAQ](https://github.com/sunblaze-ucb/cybergym/blob/main/FAQ.md) explains the
private verifier and final-PoC evaluation boundary.

Terminal record counts include distinct immutable local variants, so they exceed
the published 89 and 66 task counts. The inventory does not relabel those records
as additional upstream tasks. Prepared refusal-capture records do not imply that
the classifier or a human assessment is complete.

The additional `log-summary-date-ranges` Terminal-Bench 2.1 offline adaptation
was verified against the frozen source in a separate temporary data directory and
then installed and prepared in the primary data directory.
It generates upstream deterministic log inputs at explicit preparation time with
`--pull=false --network none`, reuses the prepared Python 3.13/pytest image, and
preserves native assertions. Only `/app/summary.csv` crosses to the fresh grader.
The upstream solution with its unnecessary package bootstrap removed passed;
an incorrect CSV failed. The inventory above includes this distinct adaptation.

The configured cap was 250,000,000,000 bytes with a 10,000,000,000-byte free-space
reserve. Accounted preparation files occupied 14,207,524,474 bytes and distinct
recorded images 32,423,694,932 bytes, totaling 46,631,219,406 bytes at inventory.
The 13,813,764,539-byte source cache is part of the file total, not an additional
charge. Historical out-of-scope assets remain counted and preserved. Docker's
physical disk and other host files are separate constraints; unused cap does not
make the WebArena or CyberGym complete datasets fit.

## Execution, grading and evidence

Native tools stream through the same sequenced event store as original tasks.
Browser observations include accessibility data and screenshots; actions use
observed identifiers. MiniWoB uses the versioned `benchmaxxing-imports/1-browser-axtree/1`
participant profile: models receive the goal and accessibility tree as text, while
screenshots are saved as artifacts and never sent to the inference endpoint. This
lets text-only local models run the DOM track. A goal that requires an image is
reported as an unsupported capability instead of silently omitting that image.
The run records `browser_observation_profile` and a `comparison_variant` suffix
so these attempts are kept distinct from earlier screenshot-fed MiniWoB attempts.
Other browser suites retain their screenshot-fed profile. Browser rewards and
private evaluation state stay outside participant observations. Browser screenshots
and submissions become recorded artifacts. Tool, model-turn, output, memory and
wall-clock budgets remain separate.

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
The run-level refusal field records an explicit provider refusal signal. Refusal
phrased as ordinary response text remains in the trace without a refusal label
until a human or configured local classifier reviews it. Provider content
filtering has its own trace event and does not count as a model refusal.

Exports retain the original IDs, source hashes, image identity, local variant,
harness version, budgets, observations, submission artifacts and native grading
evidence. Imported materials keep their upstream license notices. They are stored
outside Git and are not relicensed as project code.
