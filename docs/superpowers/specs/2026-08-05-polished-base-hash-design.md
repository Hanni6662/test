# Polished Prompt Base Hash Design

## Goal

Bind every accepted AI-polished prompt to the exact deterministic English prompt and compiler version that produced it. A polish whose binding cannot be verified must never be displayed, copied, restored, saved into a new version, or exported as valid.

## Data contract

`image_designs` and `prompt_versions` gain nullable `polished_base_prompt_hash` columns. Startup compatibility migration adds each column idempotently. The canonical binding is lowercase SHA-256 of `${compilerVersion}\0${compiledEnglish}`.

Accepted polish writes `polished_prompt` and its base hash in the same transaction. Content-changing design edits, confirmed product-fact changes, and selected custom-module edits or disabling clear both fields together. Legacy rows that contain a polish but no hash remain immutable in storage but are invalid by definition.

## Validation boundaries

`saveVersion` recompiles inside its transaction and compares the draft hash with the canonical hash of the current deterministic English prompt. A mismatch atomically clears both draft fields and writes `null` for both fields in the new version.

`restoreVersion` restores polish only when the source hash matches the source version's compiler version and deterministic snapshot and also matches the current compiler version and live deterministic prompt after the design snapshot is restored. Otherwise the restored draft and new version contain no polish binding.

Version DTOs expose the stored fields plus `polishedPromptValid`. History renders and copies polish only when this flag is true; a stored but invalid legacy polish is labelled unverifiable. Safe export emits the polish and hash only when valid and otherwise emits both as `null` with `polishedPromptValid: false`.

## Testing

Tests cover idempotent legacy migration, accepted hash persistence, paired invalidation, self-healing `saveVersion` after a direct selected-module/compiler dependency drift, guarded restore, safe legacy export, and client/UI validity selection. Existing API, persistence, compilation, E2E, production build, lint, typecheck, and audit gates remain green.
