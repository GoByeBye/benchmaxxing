# Project roadmap

Current scope prioritizes software engineering, defensive cybersecurity, frontend
UI design, offline world-knowledge retention, and needle-in-haystack retrieval.
Isolated application security labs remain a future priority after their runtime
and isolation review. Numerical engineering remains in scope, including
electrical, thermal, mechanical and numerical reasoning. Historical data from
removed providers remains available by exact run and artifact ID.

The installed original core catalog contains 126 tasks and 961 grading checks:
27 programming, 20 numerical engineering, 15 defensive security, 12 terminal and
8 synthetic policy/refusal tasks (82 tasks and 681 checks), plus eight closed-book
knowledge tasks with 64 checks and 36 context-retrieval tasks with 216 checks.
Nine original frontend-design briefs are two separate provider suites; they enter
the local task catalog after explicit bundled-source fetches and become runnable
after Chromium preparation.
Catalog search, bounded parallel model attempts,
coverage, live activity and resumable inspection are implemented. Provider
integrations have separate published, imported and prepared counts; a catalog
entry or source download does not establish a working native runtime. See
[provider preparation](providers.md), [provider validation](provider-validation.md)
and [validation](validation.md) for evidence and limitations.

| Priority | Current state and next exit condition |
| --- | --- |
| Software engineering | The 27 programming and 12 terminal tasks have isolated execution and independent fixtures. Expand original repository and repair workflows while preserving upstream identities for imported suites. |
| Cybersecurity | Fifteen defensive analysis tasks remain active, including a synthetic container-isolation policy audit. The unfinished original application labs are archived and cannot be prepared or run; complete isolation, reset and independent grading review before any release. |
| Frontend UI design | Nine original briefs across two suites have offline Chromium functional checks, desktop/mobile screenshots and a separate unscored visual review rubric. Add a distinct human review workflow before reporting visual quality scores. |
| World-knowledge retention | Eight original closed-book tasks contain 64 exact-answer checks with dated source references. Broaden topic and language coverage through newly versioned, reviewed questions. |
| Needle-in-haystack retrieval | Thirty-six deterministic profiles contain 216 checks across 32/128/512 KiB contexts and five profile families. Add separately graded evidence-location targets and token-aware coverage without relabeling bytes as tokens. |
| Numerical engineering | Retain existing math tasks and expand unit-aware physical/numerical models with explicit tolerances, conservation checks and good/bad fixtures. |
| Platform maturity | Bounded parallel workers and model comparisons are implemented. Add external-agent adapters, versioned regrading and transferable pack/result bundles as working integrations. Preserve Windows/Linux support; macOS remains unverified future work. |

The knowledge and retrieval packs use the shared CLI/HTTP run core and require
explicit local preparation. The frontend briefs require an additional prepared
browser image; source presence alone does not make those tasks runnable. Their
validation evidence is recorded in [knowledge and retrieval](knowledge-retrieval.md)
and [frontend benchmarks](frontend-benchmarks.md).

## Evaluation rules

- Compare matching task, harness, tool, budget and network conditions. External
  agents are separately identified systems.
- Preserve hashes, versions, generation settings, traces and available runtime
  provenance. Report unsupported tasks and infrastructure errors separately.
- Separate task correctness, refusal/compliance, functional UI behavior, visual
  quality and subjective review. Keep repeated attempts and uncertainty visible.
- Preserve submissions for new versioned assessments; never overwrite old grades.
- Retain upstream licensing and label adaptations that change scoring conditions.
- Preparation may download approved dependencies. Measured execution and grading
  require no external network; inference retains its explicit local/API boundary.

## Operator deferrals

`DATA_DIR/imports/operator-deferrals.json` is an optional workspace-local marker
with `schema_version: 1` and a `deferred_suites` object mapping suite IDs to reasons.
Preparation rereads it at runtime entry. A deferral fails the preparation job
with an explicit operator reason. Already compiled code in an existing service
cannot acquire new checkpoints until it loads the new module; verify a legacy
job's terminal state before claiming that preparation has stopped.

Shared hosting, distributed farms, managed model runtimes and operating-system/VM
tasks remain separate future projects. The code remains AGPL-3.0-only; third-party
content retains its own licensing.

## Defensive lab boundary

The active security packs are original, offline function tasks: policy evaluation,
configuration and log analysis, normalization and consistency checks. Their literal
expected answers stay in the controller, and candidate submissions execute in fresh
restricted grading containers. The container-policy exercise checks a published
synthetic specification; passing it does not verify Docker or host isolation.

Application security labs have no active release claim. Before reconsidering them,
require reviewed network isolation, credential exclusion, disposable state reset,
resource and output bounds, deterministic independent graders, cleanup on timeout
and cancellation, and evidence that private scoring material never enters the
candidate workspace. Existing static function coverage does not satisfy those gates.
