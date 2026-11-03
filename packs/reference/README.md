# Reference pack: python.interval-merge / 1.0.0

This original AGPL-3.0-only pack is shipped as package data under
`benchmaxxing/reference`. Its public task, starter solution and example tests are
hashed together. It exercises the complete isolated execution and grading path;
its scores are infrastructure checks, not evidence of general model capability.

The environment is a prepared `python:3.13-slim` Linux image, pinned to its exact
local image ID during explicit preparation. Runtime never pulls an image.

Participants receive TASK.md, solution.py and public_tests.py. The final grader
sends individual black-box inputs over stdin to candidate code in a fresh
container. Expected answers and checking code remain outside that container.
The final checks cover empty, singleton, disjoint, overlapping, touching, nested,
negative and large-integer intervals, plus input preservation.

Preparation records the task hash, image ID/digests, platform and preparation
time. Future packs will use versioned manifests; this foundation deliberately
ships a single typed reference pack instead of an unimplemented plugin manager.
