# Changelog

## 0.1.0 alpha 3

Corrected public alpha candidate after transactional upgrade acceptance testing.

### Fixed

1. Upgrade rollback backups now live under installer owned metadata instead of active plugin and skill discovery roots.
2. Existing alpha 2 sibling backups are removed only after the replacement installation state is durable and recoverable.
3. Added regression coverage for safe backup placement, legacy state migration, rollback restoration, and discovery root cleanliness.

## 0.1.0 alpha 2

Corrected public alpha candidate after owner first use acceptance testing.

### Fixed

1. First canonical publication now creates its owned `notes/YYYY/MM` and `state` directories through descriptor anchored safe filesystem operations.
2. Added regression coverage for first publication into a fresh journal root.

## 0.1.0 alpha 1

Initial public alpha of the evidence backed activity journal for Hermes.

### Included

1. Explicit consent and profile and platform scope.
2. Read only multi profile SQLite collection.
3. Bounded evidence chunks and resumable digest receipts.
4. Credential redaction, PII masking, and nonreversible public coverage references.
5. Evidence validated Markdown notes.
6. Conversational journal skill, 11 deterministic journal tools, four restricted generation tools, and the `hermes journal` CLI.
7. Transactional installation, restore, uninstall, and interrupted activation recovery.

### Known limitations

1. Linux and macOS are the only supported operating systems.
2. Secret detection is defense in depth and cannot prove that every sensitive value was removed.
3. Deleted or unavailable Hermes history cannot be reconstructed.
4. Generation requires a configured Hermes model and can incur provider cost.
5. Retention automation is not included. Purge is explicit and previews by default.
