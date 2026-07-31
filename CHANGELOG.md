# Changelog

## 0.1.0

Promotes the reconciled alpha line to the first stable release. Stable 0.1.0 keeps the strict alpha 5 filesystem and receipt contract while restoring the alpha 3 completion, recovery, and scheduling guarantees.

### Fixed

1. Archive the exact validated pending receipt bytes after canonical publication.
2. Bind the retained receipt, receipt archive, completion state, evidence manifest, and canonical note by SHA256 and filesystem identity.
3. Treat missing, malformed, linked, replaced, or incomplete completion evidence as active work.
4. Preserve a public same date recovery route when final completion persistence fails.
5. Resume active work before returning an already validated result.
6. Reject generation success while requested dates retain active pending work.
7. Make completion replay idempotent so a valid consumed receipt cannot rewrite canonical output.
8. Recheck every live completion artifact and the completion record before reporting success.
9. Reject stable named archives unless the exact Git ref contains exact stable metadata and no runtime data roots.
10. Verify checksums, inventory, and commit sidecars against the exact archive.
11. Check out, privacy scan, compile, and run all three suites against the exact release ref before artifact upload.

## 0.1.0 alpha 5

Corrects fail closed pending run inspection after independent review of the public alpha 4 candidate and records the verified Ubuntu under WSL support boundary.

### Fixed

1. Treat an unsafe pending root as active or invalid instead of silently reporting no pending work.
2. Enumerate pending receipts through a held directory descriptor without reopening the path.
3. Validate one strict production receipt schema across generation resume, completion classification, and maintenance.
4. Require complete state, manifest, canonical note, digest directory, coverage, and validation timestamp linkage before classifying a retained receipt as completed.
5. Document Ubuntu under WSL as supported while continuing to exclude native Windows.

## 0.1.0 alpha 4

Reconciles the public macOS hardened release line with the owner accepted alpha 3 installer repair and tightens completed run reporting before public publication.

### Fixed

1. Preserve macOS system root alias normalization and descriptor anchored lifecycle operations alongside isolated installer backups.
2. Keep upgrade backups under installer owned metadata so Hermes never discovers backup plugins or skills.
3. Treat a retained pending receipt as logically complete only when its matching durable state and canonical evidence validate.
4. Require generation success to leave no active pending work for any requested date.
5. Preserve both historical alpha 2 repair records in one coherent public lineage.

## 0.1.0 alpha 3

Corrected public alpha candidate after transactional upgrade acceptance testing.

### Fixed

1. Upgrade rollback backups now live under installer owned metadata instead of active plugin and skill discovery roots.
2. Existing alpha 2 sibling backups are removed only after the replacement installation state is durable and recoverable.
3. Added regression coverage for safe backup placement, legacy state migration, rollback restoration, and discovery root cleanliness.

## 0.1.0 alpha 2

Corrects macOS installation through system root aliases and the absence of Linux `/proc` descriptor paths while preserving descriptor anchored rejection of deeper symlinks. It also corrects first canonical publication after owner acceptance testing. CI checks the clean release tree before test caches are created.

### Fixed

1. Resolve only a root level filesystem alias such as macOS `/var` before descriptor traversal.
2. Bind macOS lifecycle operations to the locked Hermes home through a temporary descriptor based working directory, then restore the caller directory.
3. Preserve no follow enforcement for every remaining path component.
4. Share the same macOS root alias normalization across plugin and journal file operations and trust path validation.
5. Run the release tree privacy check before Python tests create cache files.
6. Create owned `notes/YYYY/MM` and `state` directories through descriptor anchored safe filesystem operations during first canonical publication.
7. Cover first publication into a fresh journal root with a regression test.

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
