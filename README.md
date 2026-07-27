# My Journal for Hermes

**The evidence backed activity journal for Hermes.**

My Journal `0.1.0-alpha.1` turns explicitly authorized Hermes session history into provenance bound daily Markdown notes. It combines bounded read only collection, credential redaction, deterministic evidence chunks, resumable digests, validation, a conversational journal skill, five deterministic read tools, four restricted generation tools, and the scriptable `hermes journal` command family.

This is an alpha. Review `PRIVACY.md`, `SECURITY.md`, and `THREAT_MODEL.md` before enabling collection.

## Package boundary

1. `skills/note-taking/my-journal` owns collection, evidence, privacy controls, digest receipts, validation, and note templates.
2. `skills/note-taking/journal` owns conversational journal requests.
3. `plugins/my-journal` owns five deterministic tools and `hermes journal`.
4. `install.py` owns transactional installation, restore, uninstall, and interrupted activation recovery.

Markdown under `journal/notes/YYYY/MM/YYYY-MM-DD.md` is canonical. Semantic stores are optional mirrors.

## Requirements

1. Linux or macOS.
2. Python 3.11, 3.12, or 3.13.
3. A Hermes Agent installation with skills, standalone plugins, native cron management, and noninteractive chat.
4. An IANA timezone available through Python `zoneinfo`.

Native Windows support is not claimed.

## Install

Run the installer with the Hermes home used by the target agent:

```text
python3 install.py --hermes-home /path/to/hermes/home
```

Enable the plugin:

```text
hermes plugins enable my-journal
```

Start a new Hermes process or restart the Hermes gateway. Verify:

```text
hermes journal status
```

```text
hermes journal resolve-range "last 7 days"
```

## Explicit configuration

Copy `skills/note-taking/my-journal/templates/config.json` to `journal/config.json` under the Hermes home. The shipped configuration has `enabled` set to `false`.

A valid enabled configuration must explicitly name at least one profile and platform:

```json
{
  "schema_version": 1,
  "enabled": true,
  "timezone": "America/Los_Angeles",
  "profiles": ["default"],
  "platforms": ["cli"],
  "excluded_session_ids": [],
  "privacy": {
    "redact_secrets": true,
    "pii_mode": "mask",
    "entropy_mode": "report"
  },
  "limits": {
    "max_message_chars": 4000,
    "max_tool_chars": 1200,
    "max_selected_messages": 25000,
    "max_retained_chars": 4000000,
    "max_sessions": 2000,
    "packet_chunk_bytes": 120000,
    "max_packet_chunks": 64
  }
}
```

Callers may lower compiled ceilings but cannot raise them. Secret redaction cannot be disabled.

Timezone precedence is:

1. Nonempty `MY_JOURNAL_TIMEZONE`.
2. `journal/config.json`.
3. Host local timezone.

## Commands

Read only commands:

```text
hermes journal status
hermes journal gaps "last 90 days"
hermes journal backfill-plan "last 30 days"
hermes journal preview "last 30 days"
hermes journal resolve-range "last month"
hermes journal maintenance
```

Generation commands use Hermes with the dedicated `my-journal-generation` toolset. It exposes only four bounded journal operations and excludes terminal, web, general file, delegation, messaging, MCP, and unrelated plugin tools. Generation verifies that validated canonical notes exist before reporting success:

```text
hermes journal generate 2026-07-27
hermes journal backfill "last 30 days"
```

Daily scheduling uses Hermes native cron. Setup persists the exact returned job identifier, reuses a still active recorded job, and removal targets only that identifier:

```text
hermes journal cron-setup --schedule "0 11 * * *" --deliver local
hermes journal cron-remove
```

Purge previews owned journal data without writing:

```text
hermes journal purge
```

Applying purge preserves `config.json` and requires both the apply flag and exact confirmation phrase:

```text
hermes journal purge --apply --confirm "DELETE MY JOURNAL DATA"
```

## Upgrade and recovery

Upgrade transactionally:

```text
python3 install.py --hermes-home /path/to/hermes/home --upgrade
```

Restore the previous installation snapshot. Restore refuses locally modified installed components unless force is explicit:

```text
python3 install.py --hermes-home /path/to/hermes/home --restore
python3 install.py --hermes-home /path/to/hermes/home --restore --force
```

Recover an install, restore, or uninstall interrupted by process death:

```text
python3 install.py --hermes-home /path/to/hermes/home --recover
```

Uninstall code while preserving journal data:

```text
python3 install.py --hermes-home /path/to/hermes/home --uninstall
```

Modified installed code is protected. Explicit replacement or removal requires `--force` with `--restore` or `--uninstall`. Lifecycle operations use one advisory lock per Hermes home and refuse concurrent execution.

## Privacy summary

1. Collection is disabled until explicitly enabled.
2. Profiles and platforms use explicit allowlists.
3. Public evidence replaces raw session identifiers with nonreversible coverage references.
4. Chat identifiers, thread identifiers, and absolute database paths are omitted from public evidence.
5. Credentials are redacted and common PII can be masked.
6. Model generation can send bounded evidence to the provider configured in Hermes.
7. Redaction reduces risk but does not prove complete secret removal.

## Validation contract

Completion fails closed unless manifest shape, timezone window, evidence identity, counts, coverage, privacy policy, digest receipts, required headings, provenance, and canonical date all reconcile. Canonical note and completion state publication roll back together if the final check fails. Reads return the exact validated snapshot and do not mutate completion state.

## Development and release

Run the three suites separately:

```text
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s plugins/my-journal/tests -v
python3 -m unittest discover -s skills/note-taking/my-journal/tests -v
```

The deterministic release archive is built only from a Git reference:

```text
python3 scripts/build_release.py --ref v0.1.0-alpha.1 --output dist
python3 scripts/verify_release.py --ref v0.1.0-alpha.1 --archive dist/my-journal-v0.1.0-alpha.1.tar.gz
```

See `CONTRIBUTING.md`, `COMPATIBILITY.md`, and `CHANGELOG.md` for the complete alpha boundary.
