# Repository working instructions

- Read README.md and docs/architecture.md before changing execution behavior.
- Keep one authoritative execution path shared by CLI and HTTP service.
- Candidate code runs only in isolated environments. Tests may use explicitly named fakes.
- The prepare command owns downloads. Run, doctor, exports and UI must work offline.
- Preserve versioned contracts, task identity, provenance and distinct failure states.
- Store terminal state and its terminal event atomically.
- Keep model/API credentials outside task environments, traces and exports.
- Run Ruff, mypy, Python tests and relevant frontend/container checks for changes.
- Regenerate web/openapi.json and web/src/api-types.ts when public contracts change.
- Preserve the Windows/Linux baseline and the AGPL-3.0-only license.
