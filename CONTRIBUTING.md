# Contributing

Use Python 3.13, uv 0.13.7, Node 24, and a Docker engine supporting Linux containers.
Install exact dependencies with `uv sync --locked` and `npm ci` in `web/`.
Dependency updates are explicit changes to the committed locks.

Run `uv run --locked ruff check .`, `uv run --locked ruff format --check .`,
`uv run --locked mypy`, and `uv run --locked pytest -q`. Reference Python files
are task inputs, deliberately excluded from the engine's strict type check.

After changing contracts or API routes, run `uv run --locked python scripts/export_openapi.py`,
then `npm run generate:api` and `npm run build` in `web/`. Commit both generated
schema and TypeScript types. Prepare the test browser with `npx playwright install chromium`
(on Linux, `npx playwright install --with-deps chromium` also installs system libraries).
`npm test` runs mocked browser interaction tests;
`npm run test:integration` targets a running real application using
`BENCHMAXXING_E2E_URL=http://127.0.0.1:8123`.

To run Docker integration tests, prepare a data directory, set
`BENCHMAXXING_DOCKER_TESTS=1` and `BENCHMAXXING_TEST_DATA_DIR` to its absolute path,
then run `uv run --locked pytest tests/integration -q`. Docker tests are explicitly
opt-in so a normal unit test command never downloads images or starts candidates.

Every grader needs known-good and known-bad fixtures. Changes to reference task
assets invalidate preparation hashes. Changes to measurement conditions require
task/harness/grader version changes and an explanation of comparability.

See [task pack authoring](docs/task-packs.md) for the installed JSON contract.
Run `benchmaxxing validate-packs`, refresh preparation with `prepare --no-pull`
when the local image already exists, and validate every added fixture in Docker.

Never execute candidate code on the host, add downloads to the run path, place
keys in configurations, or mount hidden grading material into a candidate.
Treat trace content as data when displaying it. New domain implementations follow
the roadmap; avoid empty managers or plugins for features that do not run yet.

Contributions are under AGPL-3.0-only. Preserve separate third-party notices and
dataset licenses. Do not commit runs, credentials, model weights, or dependency caches.
