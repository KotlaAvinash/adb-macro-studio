# Response to the Technical Security / DFIR Review (15 Oct 2026)

Every finding was verified against the current code. Confirmed defects were fixed with a regression
test each; the full suite is **49 tests, all passing** (`python -m unittest discover -s tests -v`).
"Proven" below means the changed code plus a test that fails on the old behavior and passes on the
new one.

| ID | Severity | Verified? | Action | Fix location | Test |
|---|---|---|---|---|---|
| F-01 | High | Confirmed | **Fixed** — manifest never overwritten; atomic write; hash logged | `wa_backup.reconstruct`, `_unique_path` | `test_f01_manifest_never_overwritten` |
| F-02 | High | Confirmed | **Fixed** — reject absolute/`..` paths, require containment (realpath+commonpath) | `wa_backup._find_media_file`, `_within` | `test_f02_media_path_traversal_blocked` |
| F-03 | High | Confirmed | **Fixed** — archive resource policy (count/size/ratio/encrypted) before any read | `chat_viewer._zip_policy_violation`, `load_zip` | `test_f03_zip_bomb_rejected` |
| F-04 | Medium | Confirmed (**and a 2nd site the review missed: `_wait_for`**) | **Fixed** — monotonic wall-clock deadline in both `_branch_wait` and `_wait_for` | `adb_core.MacroRunner` | `test_f04_branch_timeout_is_wall_clock` |
| F-05 | Medium | Confirmed | **Fixed** — manifest counts rendered messages; timestamps are UTC (`…Z`) and the raw epoch (`first_ms/last_ms`) is retained | `wa_backup._write_chat_zip`, `_ts_utc` | `test_f05_counts_and_utc_timestamps` |
| F-06 | Medium | Confirmed (potential) | **Fixed** — recursive macro schema validation (types, numeric ranges, nesting/size); Android package grammar enforced at load **and** in `Adb.launch` | `adb_core.load_macro`, `_validate_steps`, `PACKAGE_RE`, `Adb.launch` | `test_f06_macro_validation`, `test_f06_launch_rejects_bad_package` |
| F-07 | Medium | Confirmed (potential) | **Partially fixed** — typed `input text` values redacted in the command log | `adb_core._redact_cmd`, `Adb.run` | `test_f07_input_text_redacted_in_log` |
| F-08 | Medium | Architectural | **Partially addressed** — added `verify_case()` that recomputes every hash and reports tamper/missing. Signing / write-once storage remain **out of scope** (org infra). | `adb_core.verify_case` | `test_f08_verify_case_detects_tamper` |
| F-09 | Low | Confirmed | **Addressed** — `wa-crypt-tools` declared (optional) in `requirements.txt`; CI now compiles all modules. Version pinning / SBOM still open. | `requirements.txt`, `.github/workflows/tests.yml` | n/a |
| F-10 | Medium | Confirmed | **Partially fixed (surfaced, not resolved)** — co-visible same-name chats now raise an explicit `ambiguous-duplicate` warning so the operator can disambiguate. A durable per-chat identity (true fix) is not available from the UI reader. | `whatsapp_batch.duplicate_names`, discovery loop | `test_f10_duplicate_names_detected` |

### Additional observations
- **Decrypt can hang:** **Fixed** — `decrypt_backup` now has a `decrypt_timeout` (default 30 min), handles `TimeoutExpired`, and removes partial output.
- **`ui_dump` file fallback writes `/sdcard/window_dump.xml`:** confirmed. It is only reached in the **screen-driven export path, which is already state-modifying**; the **read-only backup path (`wa_backup`) never calls `ui_dump`**, so the read-only guarantee is not affected. A strict mode that disables the fallback is **[FUTURE]**.
- **Media/viewer basename conflation:** F-02 constrains lookups to the media root; distinct files with the same basename can still be indexed by basename in the viewer — **[FUTURE]** unique-name mapping.
- **`contacts_extract` broad `except` → zero counts:** unchanged; raw query text is preserved (`*.raw.txt`) so zero can be distinguished from error on inspection.

### Explicit disagreements / clarifications
- None of the findings were rejected. One correction: **F-04's root cause exists in two functions**
  (`_branch_wait` and `_wait_for`), not one; both are fixed.
- F-08 and F-10 are **partially** addressed by design: full remedies (cryptographic chain of custody;
  durable chat identity) need infrastructure / a device-provided identifier the current source cannot
  obtain. They are not claimed as fully fixed.

### Still open (not claimed fixed)
Tamper-evident/signed manifests and write-once storage (F-08); durable chat identity (F-10);
dependency pinning + SBOM + optional-path integration tests (F-09); a strict read-only mode disabling
the `ui_dump` file fallback; log-redaction of raw UI/text evidence captures and case-folder ACLs
(F-07 beyond typed-text); physical-device validation of the live `adb pull` path.

### Missing inputs needed to go further (requested by the reviewer and still needed)
1. An authorized **physical device** (and emulator profile) to validate the live pull/export paths.
2. A **representative, non-sensitive** decrypted `msgstore.db` fixture for CI (the real run was manual).
3. The **WhatsApp build/version** range to support (UI resource-ids and share-sheet layout are
   version-coupled: `CONTACT_ID`, `LIST_MARKER`, `.qs_coords.json`).
4. Policy decisions: acceptable archive-size ceilings (F-03), retention/ACL requirements (F-07), and
   whether a signing key / write-once evidence store is available (F-08).
