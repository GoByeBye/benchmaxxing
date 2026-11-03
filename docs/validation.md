# Foundation validation

Validated on 4 November 2026 (Europe/Oslo).

| Check | Observed result |
| --- | --- |
| Windows Python suite, including real Docker integration | 94 passed |
| Linux Python suite, including real Docker integration | 94 passed; test controller launched with `--network none` |
| CLI attachment to a running local service | Passed, score 1.0; regression test covers HTTP client reuse |
| Complete Linux CLI reference run | Passed, score 1.0; controller networking disabled |
| Mocked Playwright UI suite | 7 passed |
| Real browser/Docker workflow | Correct submission passed at 100%; incorrect submission failed; artifacts and ZIP export verified |
| Responsive visual review | Desktop and populated mobile views inspected; mobile history overflow repaired and regression tested |
| Ruff lint and format | Passed |
| Strict mypy | Passed for 11 engine/application source modules |
| TypeScript and production frontend build | Passed |
| Frontend dependency audit | Zero reported vulnerabilities at verification time |
| API schema/type generation | Regenerated successfully from the final contracts |
| Wheel packaging | Bundled UI, reference task and license verified |
| Fresh installed-wheel smoke | Installed from an explicitly prepared dependency wheelhouse with network disabled; bundled UI, real Docker run at 100%, and export verified |
| Source distribution | Explicit inclusion list; archive checker rejects runtime/private paths and verifies required assets |

Environment: Python 3.13.18, uv 0.13.7, Node 24.22.1, Docker Engine 29.9.2 through
Docker Desktop/WSL2. The prepared Linux reference image identity was
`sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1`.

The Docker tests exercise good/bad grading, workspace tools, network and filesystem
isolation, absence of host secrets/socket mounts, symlink path rejection, bounded
output/artifacts, timeouts, cancellation and owned-resource cleanup. Core tests
cover storage/export integrity, controller ownership and recovery, API token/origin
boundaries, SSE cursors, invalid configuration, model transport limits and errors.

The model adapter was tested with a real localhost mock HTTP server and an existing
local Strata instance serving `qwen3.8-flash-next-iq3_s` (engine 0.1.56, IQ3_S,
DFlash2, 262144-token context, RTX 4090). The real reference run
`ea0438250e1a41a992902bf7fa0284fb` passed all eight independent checks in
27.812 seconds across five model turns. The endpoint reported 8,390 prompt tokens
and 1,028 completion tokens, totaling 9,418. The model used command execution,
file writing and submission tools; no adapter changes were required.

The reproducible request settings are in `examples/strata-qwen.toml`: temperature
zero, 4096 maximum response tokens, 24 turns and a 600-second run budget. The
model's default reasoning behavior was used. Strata's explicit offline mode was
enabled and candidate containers had no network. The Windows host remained
connected during this smoke test, so it does not establish OS-level inference
egress isolation. Model-weight hashes and effective generation settings were not
reported by the endpoint and remain unknown in the run record. This single small
task demonstrates local tool-calling compatibility, not broad model quality.

The results above are local verification; consult GitHub Actions for current
remote CI status. The local suites pass with one upstream
Starlette TestClient deprecation warning about its HTTPX test transport. macOS
and the later roadmap milestones are not part of this validation.

An ordinary package cache was insufficient for a fresh offline installation;
the installed-wheel check used explicit locked dependency wheels instead. The
existing prepared virtual environment runs without dependency downloads. This
release does not claim a universal air-gapped installer.
