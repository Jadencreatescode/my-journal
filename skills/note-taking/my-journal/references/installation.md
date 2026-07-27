# Installation and Sharing

## Package Boundary

Share only the skill directory. Never include runtime configuration, evidence manifests, model packets, journal notes, state files, SSH details, channel identifiers, or credentials.

## Install

1. Copy the `my-journal` directory into the recipient's Hermes skills directory under the `note-taking` category, and install/enable the matching `my-journal` plugin.
2. Run `hermes journal setup-inventory` to list available profile and platform labels without reading message bodies. Databases up to 8 GiB use the normal tier. If inventory reports a larger database, require its exact profile bound phrase before approving the 16 GiB or 32 GiB tier, then repeat inventory. Databases above 32 GiB require a separate bounded sharding or indexing procedure. The guided route creates private runtime configuration only after approval. Manual operators may copy `templates/config.json` to the private journal root, but must leave it disabled while planning.
3. Select a nonempty IANA `timezone`, explicit nonempty `profiles` and `platforms` arrays, and the private journal destination. Empty allowlists cannot be enabled; `profiles` and `platforms` are arrays, not obsolete scalar settings.
4. Make an explicit privacy choice: choose `privacy.pii_mode` (`mask` or `preserve`) and `privacy.entropy_mode` (`report`, `redact`, or `off`). Secret redaction remains mandatory.
5. Run the guided setup planner. It reads eligible timestamp metadata, not message bodies, and produces a bounded historical plan containing eligible activity dates, workload counts, the maximum daily message count, the smallest sufficient 25,000, 50,000, or 100,000 tier, existing dates, and missing dates.
6. Review the exact profile/platform scope, timezone, privacy policy, date bounds, workload, and exclusions. Approve that immutable plan with its generated confirmation phrase before setting `enabled` to `true` or generating history.
7. Backfill only the approved missing activity dates, oldest first. Keep each generation run to one bounded calendar date; stop on any collection, generation, reconciliation, or validation failure rather than skipping ahead.
8. For every generated date, require zero database errors, exact session reconciliation, validator success, completion state, and readback of the canonical note.
9. Only after the approved historical backfill and a manual previous-day generation both succeed, create the daily job with `hermes journal cron-setup`. Do not schedule first and hope setup completes later.
10. Verify the created job and its first successful run before relying on the recurring schedule.

If a later date exceeds its approved daily message tier, generation stops before collector launch and returns the exact next tier phrase. Use `hermes journal workload-check <date>`, require the exact phrase, run `hermes journal workload-approve <date> --confirm <phrase>`, and retry the same date. The triggering date raises the global reusable daily tier for future dates. Targeted means the approval changes only `limits.max_selected_messages`; it does not repeat onboarding or silently change scope, privacy, database limits, or other ceilings. Dates above 100,000 eligible messages remain blocked.

Production collection validates durable evidence for every configured 16 GiB or 32 GiB database tier and every configured 50,000 or 100,000 daily tier before SQLite opens. Purge preserves `config.json` but removes owned approval receipts. After purge, rerun the relevant check and exact approval to restore evidence before generation resumes.

If approval evidence is published but configuration publication is interrupted, repeat the same exact database or daily phrase. The approval route recognizes the interrupted state and completes the missing update safely.

## Collector Example

```text
python3 scripts/collect_journal.py --home <hermes-home> --output <private-journal-root> --config <private-journal-root>/config.json --date <YYYY-MM-DD>
```

The collector discovers candidate databases but selects only the profiles explicitly approved in the nonempty `profiles` allowlist. It counts and retains only sessions whose source appears in the explicit nonempty `platforms` allowlist. Add a newly discovered profile or platform only through a reviewed configuration/plan change; there is no implicit “all profiles” or “all platforms” scalar setting.

## Validator Example

```text
python3 scripts/validate_journal.py --manifest <manifest.json> --note <daily-note.md> --state <latest-state.json>
```

The validator refuses completion when required headings, date, run ID, coverage counts, or redaction checks fail.

## Portability Notes

1. Python 3.11 or newer is recommended.
2. The scripts use only the Python standard library.
3. Hermes databases are opened in read only mode.
4. Older profile schemas are supported through optional column discovery.
5. The canonical note destination can be local Markdown, Obsidian, or another private file store.
6. Semantic indexing is optional.
7. Remote vault transport is installation specific and must stay outside the shared skill.
8. The standard collector includes all context classes. A later private or professional view should filter presentation after collection rather than destroy source coverage.

## Acceptance Test

A successful installation proves:

1. Every discovered database is reported.
2. Database error count is zero.
3. Every selected session has a packet header.
4. Every represented platform and active profile appears in coverage.
5. No internal reasoning is selected.
6. Tool text is bounded.
7. Likely credential shapes are redacted.
8. The daily note passes validation.
9. The canonical note is read back from its destination.
10. The scheduled job completes one manual run.
