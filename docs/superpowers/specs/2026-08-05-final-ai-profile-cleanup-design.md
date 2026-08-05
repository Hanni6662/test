# Final AI, profile, and quarantine hardening design

## Scope

Harden three existing boundaries without changing the product workflow: prompt polish must preserve signed and priced quantities, product-profile writes must mutate confirmed facts only, and failed quarantine unlinks must be retried safely on a later deletion.

## Prompt numeric tokens

Treat a protected numeric expression as one lexical token containing an optional unary ASCII or Unicode sign, an optional Unicode currency symbol, the number/expression, and any trailing percent sign or supported unit. Normalize whitespace only. Changing `-5` to `+5`, `−5` to `-5`, or `$10` to `€10` must fail validation.

## Product-profile API

Separate the write type from the read model. `POST /api/projects/:id/product-profile` accepts exactly `{ "facts": ProductFacts }` through a strict schema. `confirmedFacts`, `aiCandidates`, `referenceImages`, and every unknown top-level field return the existing structured HTTP 400 response. The GET response continues to include server-owned confirmed facts, AI candidates, and read-only reference images. Candidate extraction and confirmation remain the only mutation paths for their fields.

## Quarantine retry

Expose a bounded sweep for one project's fixed `.quarantine` directory. It lists only that directory, unlinks only regular entries whose basename matches the generated UUID `.delete` convention, does not recurse, and never follows caller-supplied paths. Every upload-backed image deletion runs the sweep before creating its new quarantine file. A failed sweep is best-effort and must not block the authoritative deletion flow; a later deletion retries the orphan.

## Verification

Use red/green tests for signed/currency token mutations, strict profile requests, and a final-unlink orphan removed by the next deletion. Run the full test suite, typecheck, lint, production build, dependency audit, and Git whitespace check before committing implementation.
