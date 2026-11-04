# Offline knowledge and long-context retrieval

These original packs add **44 task profiles and 280 question checks**:
eight closed-book knowledge tasks with 64 questions, and 36 supplied-context
retrieval tasks with six questions each. They use `benchmaxxing-answer/1` and
`answer-exact/1`, separately from workspace coding and imported native harnesses.
The original 81 task identities are preserved.

## Closed-book retained knowledge

The eight topics are solar-system facts, SI units, chemical symbols, oceans and
water processes, molecular genetics, early European printing, historical US
constitutional structure, and UN history/languages. Questions are original;
accepted facts refer to primary institutional sources checked on 4 November 2026.
Each question records its reference IDs, accepted answers and normalization rule.

References include [NASA](https://science.nasa.gov/solar-system/planet-sizes-and-locations-in-our-solar-system/),
[NIST](https://www.nist.gov/si-redefinition/definitions-si-base-units),
[IUPAC](https://iupac.org/what-we-do/periodic-table-of-elements/),
[NOAA](https://oceanservice.noaa.gov/facts/biggestocean.html),
[NHGRI](https://www.genome.gov/about-genomics/fact-sheets/Deoxyribonucleic-Acid-Fact-Sheet),
[Library of Congress](https://loc.gov/exhibits/bibles/the-gutenberg-bible.html),
[National Archives](https://www.archives.gov/founding-docs/constitution/what-does-it-say),
and the [United Nations](https://www.un.org/en/about-us/history-of-the-un/preparatory-years).
Only independently phrased questions and factual scoring values are distributed;
third-party prose, illustrations and whole datasets are not copied or relicensed.
Original pack material is AGPL-3.0-only. Source web-page bytes were not archived,
so a page-content hash is not claimed.

The participant receives only a fixed system instruction and the questions.
References, accepted answers, corpus files and tool definitions are absent from
the request. This measures retained factual recall under the declared harness,
not browse/search ability or persistent memory across sessions. It is a small,
mostly elementary, English-language selection with limited geographic and topic
coverage. It is not a comprehensive world-knowledge score or a claim of
contamination resistance. Curating more topics requires original questions,
reviewed accepted answers and a new immutable task hash.

## Needle-in-a-haystack profiles

Synthetic contexts contain exactly **32,768, 131,072 or 524,288 UTF-8 bytes**.
The ASCII ledger generator is deterministic and versioned `synthetic-ledger/1`.
These are byte lengths; tokenizer tokens remain unknown unless reported by the
inference endpoint. A byte length must not be relabeled as a token context size.

| Profile | Tasks | What changes |
| --- | ---: | --- |
| Single needle block | 15 | One block with six requested key/value pairs; anchors at 5%, 25%, 50%, 75% and 95% of filler, at all three lengths |
| Multiple needles | 9 | Six separately placed records, with three anchor arrangements at each length |
| Two-hop join | 6 | Resolve six aliases to canonical keys, then retrieve values located elsewhere; two anchor arrangements at each length |
| Absent needles | 3 | Historical near-matches exist but no canonical answers; null is correct |
| Instruction distractors | 3 | Ignore instructions embedded in the supplied ledger while finding canonical values |

Every profile includes obsolete historical near-matches. Seeds are fixed per
profile and recorded in task identity and evidence; repeated runs are trials on
the same frozen input. Actual record byte offsets are recorded, including any
shift introduced by other inserted records. An anchor fraction describes the
insertion into filler, rather than falsely claiming an exact final token position.

The entire context is sent in the model's user message. No `read_file`, shell,
browser or search tools are supplied. Large contexts are never silently cropped,
summarized, split across model requests or replaced by file search. A server that
cannot accept a profile must report its error; use shorter separately identified
profiles when appropriate. Tool-assisted file retrieval would be another track.

## Grading and runtime

One model response must be a JSON object containing exactly the requested IDs.
Each value is a string or null. Duplicate fields, extra/missing IDs, nonfinite
values, arrays, Markdown fences and executable text fail format validation.
Text facts use Unicode NFC, case-folding and whitespace normalization against
explicit alternatives. Chemical symbols and generated retrieval codes match
literally. There is no substring, keyword, semantic or model-judge scoring.

Correctness uses all questions as its denominator. An unknown null for a known
answer is recorded separately from a wrong non-null answer and contributes no
correctness credit. Null for a deliberately absent needle is correct. Explicit
provider refusals remain a separate run field. Malformed model transport,
cancellation, wall/token exhaustion and incorrect answers remain different states.

These tasks process input/output as data and expose no candidate code tools, so
they do not start a candidate container. Explicit preparation still registers the
exact installed task hashes and checks the selected local preparation image;
execution performs no download or installation. The default task wall budget is
30 minutes. Response bytes, response tokens and total stored artifact bytes remain
bounded independently. Answer profiles use one inference request even when the
configured model-turn ceiling is larger.

This establishes the harness boundary, not an attestation that an independently
operated inference server disables its own retrieval or external networking.
Offline deployments must configure and validate that server separately.

## Evidence and reproduction

Runs preserve the exact system and user prompts as checksummed artifacts, the
response, task hash, harness/grader versions, endpoint configuration, requested
generation settings, available usage and timing. Retrieval metadata identifies
the context byte range inside the prompt artifact, its SHA-256, generator, seed,
length and record positions. The full context can therefore be inspected without
duplicating it or relying on a currently installed generator. Hidden accepted
answers remain outside public catalog records, prompts and input artifacts.
Source URLs and the factual check date are recorded as provenance, rather than
implying a downloaded source archive or an official upstream suite identity.

```sh
benchmaxxing prepare --no-pull
benchmaxxing tasks --category knowledge
benchmaxxing run --task knowledge.genetics --variant good
benchmaxxing run --task knowledge.genetics --variant bad
benchmaxxing run --task retrieval.single-32k-p50-s4817 --variant good
benchmaxxing suite --category retrieval --config examples/local-model.toml
```

Specify the same `--data-dir` for preparation, CLI and service. Register new packs
when no controller owns that directory, or use a separate prepared directory while
another suite is running. Scripted fixtures validate plumbing and graders; they
are never presented as model-quality measurements.

## Recorded validation

On 4 November 2026, 103 answer-pack checks passed on Windows, including real local
HTTP requests verifying that tool fields are omitted and the entire supplied
context reaches the endpoint. A combined run with existing pack, catalog, model
and streaming regressions passed 189 checks. Cases cover all good/bad answer
fixtures, exact deterministic contexts, absent/multiple/two-hop needles, format
errors, case-sensitive codes, unknown answers, cancellation, deadline/token/output
limits, malformed model transport and rejection of unexpected tool calls.

A separate explicitly prepared directory exercised every one of the 44 profiles
through the shared execution core, with 44 correct and 44 deliberately incorrect
fixture attempts. It then verified a CLI run attached to an ephemeral local
service, API/CLI record agreement, another API-created retrieval run, and every
size/SHA-256 in an exported bundle. The service shut down and removed its
controller descriptor. Preparation reused local image
`sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1`
with pulls disabled. These answer runs started no candidate containers. The host
remained connected; no host-wide egress isolation or additional Qwen results are
claimed. A comparison against the primary prepared manifest confirmed that all
81 earlier original task hashes remained unchanged.
