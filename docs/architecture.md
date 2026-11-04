# Architecture

Benchmaxxing is a single-operator local application. The installed Python package
owns the execution engine, Typer CLI, FastAPI service, packaged reference task,
and built web assets. React/TypeScript is a frontend, not a second scheduler.

## Run lifecycle

`validate → verify preparation → create candidate → participant/tool loop → collect → independent grade → cleanup → finalize`

RunSpec, TaskSpec, RunEvent, ScoreCard and artifact records are typed, versioned
Pydantic contracts. Model configuration is validated before persistence. The
foundation implements the scripted fixture and an OpenAI-compatible model using
the versioned built-in harness. `benchmaxxing-tools/2` adds task-aware instructions
and designated Python/JSON submissions; old `/1` results retain their identity.
External-agent contracts will be introduced
with an executable integration in milestone 3, rather than shipped as unused stubs.

The controller holds a cross-thread OS file lock for one data directory and owns
a single worker. CLI commands attach to the local service when available or own
a headless controller for the command lifetime. Active and queued runs share SQLite.
An active controller prevents preparation from replacing its environment record.

SQLite is authoritative for state and sequenced events. Terminal state and its
terminal event commit together. SSE reconnects using Last-Event-ID or an `after`
cursor. Artifact files use generated IDs, exclusive creation and SHA-256 checks.
Exports are allowed only after a run finishes and include a checksum manifest,
run.json, events.jsonl, artifact metadata, and immutable artifact bytes.

Each event transaction also updates a compact `run_progress` projection. The
`/api/v1/activity` endpoint reads only queued/running records and this projection,
without loading model conversations. Actual start time excludes time spent queued;
model turns and completed grading checks come from execution events. The UI polls
this snapshot about once per second while resuming the selected trace by sequence.
SSE reads bounded batches off the asynchronous server loop and drains all committed
events before sending completion. The projection is additive to existing databases;
run records and event traces remain the authoritative evidence.

On startup, unfinished runs become interrupted. Cleanup only targets containers
with both Benchmaxxing's managed label and the exact workspace identity. Failed
recovery is retried before accepting a new run. Cancellation stops execution;
re-running creates a new attempt rather than pretending to resume a model's state.

## Preparation and isolation

`prepare` explicitly pulls the chosen reference image, inspects its immutable ID,
and records its digests, architecture and task hash. Every run validates this
record and uses the exact image with pulls disabled. Preparation records a hash
for each installed task, including public assets, hidden checks and reference
fixtures. Updating a pack requires explicit preparation again; `prepare --no-pull`
registers the catalog against an existing image offline. The original packs need only the Python base image;
there are no runtime package installs.

Candidate containers run as an unprivileged user, with dropped capabilities,
no-new-privileges, read-only root, small writable tmpfs directories, and CPU,
memory, PID, file-size, time and output bounds. They receive no host filesystem
mounts, credentials, Docker socket, or external network. File tools validate
workspace paths; all candidate content is interpreted only inside Docker.

The reference grader keeps expected answers in the controller and runs the
captured submission in a fresh candidate container. It sends black-box inputs and
checks bounded observations on the host. Public examples are not hidden grading
material. The reference benchmark is deliberately small and demonstrates plumbing,
not broad model quality or resistance to benchmark memorization. Additional Python
packs call a declared single-argument function and compare typed JSON observations
on the host with explicit numeric tolerances. Terminal and policy tasks submit
bounded JSON artifacts, which are parsed as data and compared on the host. JSON
artifacts never execute on the host. Policy decision artifacts require exactly
their declared fields; duplicate object keys and nonfinite numbers are rejected.
Original public inputs are snapshotted into checksummed artifacts before execution.

## Inference and provenance

Offline profiles accept loopback endpoints only. Localhost is canonicalized, URL
credentials/query/fragment are rejected, and requests disable ambient proxies and
redirects. API profiles explicitly permit remote HTTP(S) endpoints. Credentials
are loaded from named environment variables, never placed in task environments.
Responses and recorded errors are bounded and redacted for the configured key.

A loopback server can itself call the internet. A genuinely offline deployment
also configures its inference runtime accordingly and validates it disconnected.
Benchmaxxing does not install, manage, or certify third-party model servers.

Records distinguish model-with-harness from scripted fixtures, capture requested
generation settings and returned model IDs, and keep unavailable usage/effective
settings/model provenance unknown. Full prompts, tools and responses are retained.
Task failure, infrastructure error, unsupported capability, interruption,
cancellation and budget exhaustion remain distinct; explicit refusal is separate.

## API and UI boundary

The service binds to 127.0.0.1. Mutations require a session token; Host, Origin and
browser cross-site checks prevent unrelated web pages from controlling the local
service. There are no user accounts or LAN hosting in the foundation. Artifacts
are addressed by recorded IDs, outputs render as text, and assets are bundled.

API documentation is generated into web/openapi.json; frontend types are generated
from it. Node is needed to develop/build the UI, not to run a built wheel.

## Decisions and limits

- Python integrates task tooling and scientific/domain libraries; TypeScript keeps
  the browser interface typed. One process and SQLite suit the current operator model.
- Windows/Linux hosts share Linux candidate images. Native Windows tasks, desktop
  GUIs, VM/kernel labs and distributed workers require separate future backends.
- Tasks designate either solution.py or submission.json. Public input snapshots
  accompany new pack runs. Multi-file code submissions and richer domain artifact
  formats remain future work.
- Installed JSON task definitions are schema-validated and loaded without importing
  task code. Pack additions do not require controller changes. The CLI and UI queue
  independent repeated attempts through the same single worker.
- Refusal packs grade synthetic policy decisions, not free-form safety judgments.
  Expected/observed decisions are recorded separately from explicit provider refusal
  and correctness. Defensive security tasks are pure local analyses, not live labs.
- Objective scores are separate from future human/model reviews. There is no
  universal leaderboard or implied model ranking from the foundation fixture.
