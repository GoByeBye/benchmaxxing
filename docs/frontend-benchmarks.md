# Original frontend design benchmarks

The `frontend-design` suite contains six original, versioned Benchmaxxing briefs.
The sibling `frontend-design-extended` suite adds three briefs at revision 1.0.0.
Its separate source and grader preserve the original six source hashes, task IDs
and task hashes; fetching the extension never rewrites the original pack.
Frozen graders use canonical line endings on every host: legacy v1 CRLF preserves
its published identities, while the extension uses LF.
They measure implementation of an interface from a written specification, using
HTML, CSS and JavaScript in a single self-contained `index.html`. They are not
imported leaderboard tasks or browser-control-only exercises.

| Brief | Objective browser observations |
| --- | --- |
| Order operations dashboard | Case-insensitive customer search, combined status filtering, visible rows and totals |
| Support request form | Accessible invalid-email feedback, keyboard submission, success announcement and retained input values |
| Workspace tabs | Tab/panel relationships, selected state, roving tab order, arrow/Home/End selection and focus |
| Project confirmation dialog | Accessible modal name, initial focus, keyboard containment, Escape restoration and confirmation |
| Product navigation | Desktop links, mobile expansion state, keyboard link activation, Escape restoration |
| Appearance preferences | Named toggle, visible foreground/background changes, persisted theme after reload |
| Quantity and delivery cart (extended) | Live totals, delivery cost, invalid quantity feedback, retained total and keyboard update |
| Shipping help accordion (extended) | Single expanded answer, controlled regions, Enter/Space toggling and retained focus |
| Paginated reading list (extended) | Ordered page content, page announcement, native boundary disabled states and keyboard navigation |

All briefs also check document language, title, main landmark, viewport metadata
and horizontal overflow at desktop and mobile dimensions. These are specific
observations, not a comprehensive accessibility certification. Each extension brief
repeats its complete behavioral sequence from a fresh document at both widths.
No model judge is used; the visual rubric remains unscored.

## Preparation and execution

```sh
benchmaxxing providers fetch frontend-design
benchmaxxing providers prepare frontend-design
benchmaxxing providers fetch frontend-design-extended
benchmaxxing providers prepare frontend-design-extended
benchmaxxing tasks --category frontend
```

Fetching installs bundled original source data and needs no internet. Runtime
preparation explicitly reuses or builds the pinned BrowserGym/Chromium image used
by MiniWoB, launches a real offline Chromium probe, and records immutable image
and task identities. Execution verifies those records and never downloads missing
dependencies. The preparation cap accounts for source files and registered images.

Select an installed frontend task in the UI and configure a model endpoint. The
native-task defaults supply 2 GiB memory, 256 PIDs and a 64 MiB artifact budget.
For a deterministic CLI fixture, use `examples/frontend-scripted.toml` and pass
the installed task ID with `--task`. Set `scripted_variant = "bad"` to verify an
incorrect submission. Each task has good and deliberately unfinished fixtures.

The candidate can edit its isolated workspace and run local commands. Only
`index.html` crosses into a fresh grader container. Private checks are never
present in the candidate container. The grader serves only that HTML file from
an ephemeral loopback HTTP server and drives Chromium with external requests
blocked; the entire container also has `--network none`. No host filesystem,
controller credentials or Docker socket enter either environment.

## Scores and design review

Functional checks determine the objective score and pass/fail result. Desktop
1280px and mobile 390px PNG screenshots become independent, checksummed run
artifacts alongside the submission and grading evidence. The UI previews these
images through their recorded artifact IDs; it never renders submitted HTML.

Visual hierarchy, spacing, typography, component coherence and responsive
composition have a separate **needs review** rubric with a null score. A passing
functional result does not invent an aesthetic score. Reviewers can inspect the
preserved screenshots without rerunning inference. Upstream/native grader trust
assumptions still apply: browser execution is isolated, but these checks do not
claim resistance to every adversarial grader or browser attack.

Validated on 4 November 2026: all six good fixtures passed and all six unfinished
fixtures failed in fresh networkless browser graders. The three extension good
fixtures also passed and their three unfinished fixtures failed; three targeted
behavior mutations were rejected at both viewport widths. Shared-runner checks
verified two recorded PNG artifacts and the native grading JSON for each pack. Protocol tests
reject invalid artifact hashes, traversal names, unsupported media types,
duplicates and over-budget outputs. The suite requires no GPU or model weights.

For local UI development on Windows, `npm run test:edge` runs the Playwright
checks in an already installed Microsoft Edge when Playwright's managed Chromium
binary is unavailable. The benchmark grader still uses the pinned offline
Chromium image recorded during `providers prepare`.
