# Evidence Manifest Schema

## Purpose

The evidence manifest is a private, bounded record used to generate and verify one journal entry. It is not the readable journal and should not be published with the shareable skill.

## Top Level Fields

1. `schema_version`: Manifest format version.
2. `run_id`: First 16 hexadecimal characters of the canonical evidence SHA256.
3. `evidence_sha256`: Full SHA256 of the canonical collected evidence before creation metadata is added.
4. `journal_date`: Local calendar date represented by the run.
5. `created_at`: UTC creation timestamp.
6. `window`: Half open UTC timestamp range.
7. `coverage`: Database, session, message, platform, and profile counts.
8. `databases`: One status record for every selected authorized database.
9. `sessions`: One record for every selected session.

## Session Fields

1. `profile`
2. `platform`
3. `session_id`
4. `title`
5. `started_at`
6. `chat_id`
7. `thread_id`
8. `display_name`
9. `coverage_ref`: Opaque SHA256 of profile plus session identifier, used for exact grouped digest reconciliation without exposing raw identifiers in the readable journal.
10. `context_label`
11. `messages`

The default context label is `unclassified`. The journal writer assigns professional, personal, mixed, or unclear while retaining every session.

## Message Fields

1. `message_id`
2. `role`
3. `timestamp`
4. `content`
5. `tool_name`
6. `tool_calls`

System and developer messages are counted for coverage but not copied into evidence. Reasoning columns are never selected. Content and tool payloads are redacted and bounded.

## Coverage Invariant

Every database is reported even when unreadable. Every session with at least one selected message receives a session record and opaque coverage reference. The readable note may group sessions, but every digest must declare the exact run ID and evidence SHA256, and every reference must appear exactly once across the note and its declared digest directory. Provenance must exactly match the evidence hash, manifest path, digest directory, database status, counts, platforms, and profiles. Completion is blocked when any database reports an error.
