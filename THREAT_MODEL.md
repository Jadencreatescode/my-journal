# Threat Model

## Protected assets

1. Hermes session history.
2. Journal evidence and Markdown notes.
3. Credentials and personal identifiers contained in session text.
4. Completion state and provenance integrity.
5. Installed Hermes skills and plugin code.

## Considered attackers

1. A local process able to replace files or directories inside writable roots.
2. Malicious or malformed session content.
3. A crafted SQLite database with expected tables and hostile values.
4. A tampered package, validator, digest, manifest, or release archive.
5. Oversized data intended to exhaust memory, storage, context, or model cost.

## Controls

1. Descriptor anchored reads, directory creation, writes, renames, and removals with symlink refusal.
2. Read only SQLite access through an anchored file descriptor, with a compiled database size ceiling and bounded SQL extraction before redaction.
3. Compiled discovery, raw field, collection, and output ceilings that callers cannot raise.
4. Mandatory secret redaction and declared privacy policy validation.
5. Deterministic chunk identities and resumable digest receipts.
6. Exact coverage validation before completion state is committed.
7. Transaction records and rollback for installation lifecycle operations.
8. Deterministic release archives built from a Git reference.
9. Unattended generation receives only four dedicated journal generation tools. It has no terminal, web, general file, delegation, messaging, MCP, or unrelated plugin tools.
10. Packet text is structurally labeled untrusted data. Canonical note and completion state replacement roll back together if validator publication fails. The durable completion chain then binds and rechecks the receipt, archive, state, evidence manifest, and canonical note before success.
11. Cron ownership intent is durably written before native job creation. Exact structured reconciliation, directory descriptor locking, and descriptor anchored purge prevent orphaned or misidentified automation.

## Outside scope

1. A fully privileged operating system administrator.
2. Compromise of the selected model provider or Hermes itself.
3. Recovery of deleted or unavailable session history.
4. Proof that regex and entropy redaction catches every possible secret.
5. Native Windows support. Ubuntu under WSL is covered by the Linux support path.
