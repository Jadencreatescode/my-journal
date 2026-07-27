# Installation and Sharing

## Package Boundary

Share only the skill directory. Never include runtime configuration, evidence manifests, model packets, journal notes, state files, SSH details, channel identifiers, or credentials.

## Install

1. Copy the `my-journal` directory into the recipient's Hermes skills directory under the `note-taking` category.
2. Copy `templates/config.json` to a private runtime directory outside the skill.
3. Set the recipient's timezone and private journal destination.
4. Keep `scope` set to `all` for the standard version.
5. Run the collector for one bounded historical date.
6. Require zero database errors and exact session header reconciliation.
7. Generate a test note from the packet.
8. Run the validator and require a completed state file.
9. Install a daily agent cron job using `templates/cron-prompt.md`.
10. Run the job manually once before relying on its schedule.

## Collector Example

```text
python3 scripts/collect_journal.py --home <hermes-home> --output <private-journal-root> --date <YYYY-MM-DD> --timezone <IANA-timezone>
```

The collector discovers the default database and every profile database. It does not use a platform allowlist. Discord, Telegram, CLI, cron, API, Slack, SMS, and future adapters are included whenever their source value appears in the selected Hermes sessions.

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
