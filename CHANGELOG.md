# Changelog

## 0.1.0 alpha 2

Corrects macOS installation through the system root aliases used by temporary directories while preserving descriptor anchored rejection of deeper symlinks. CI now checks the clean release tree before test caches are created.

### Fixed

1. Resolve only a root level filesystem alias such as macOS `/var` before descriptor traversal.
2. Preserve no follow enforcement for every remaining path component.
3. Run the release tree privacy check before Python tests create cache files.

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
