# My Journal Plugin

My Journal adds deterministic journal operations to Hermes while leaving semantic summaries to the conversational `journal` skill.

## What it provides

The plugin registers these model tools:

* `journal_status`
* `journal_resolve_range`
* `journal_read_entries`
* `journal_find_gaps`
* `journal_plan_backfill`
* `journal_setup_inventory`
* `journal_setup_database_approve`
* `journal_daily_workload_check`
* `journal_daily_workload_approve`
* `journal_setup_plan`
* `journal_setup_approve`
* `journal_recurring_issues` (read only)
* `journal_lesson_record` (appends to the lesson ledger only)
* `journal_improvement_report` (read only)

Read tools use the `journal` toolset. Unattended generation uses a separate `my-journal-generation` toolset containing only:

* `journal_generation_collect`
* `journal_generation_resume`
* `journal_generation_get_chunk`
* `journal_generation_record_digest`
* `journal_generation_complete`

The generation toolset does not include terminal, web, general file, delegation, messaging, MCP, or unrelated plugin tools.

It also registers the scriptable command family:

```text
hermes journal status
hermes journal gaps [range]
hermes journal backfill-plan <range>
hermes journal resolve-range <range>
hermes journal recurring [range] [--min-dates N] [--max-groups N]
hermes journal improvements [--lesson-id ID] [--as-of YYYY-MM-DD]
hermes journal generate <date>
hermes journal backfill <range>
hermes journal setup-inventory
hermes journal setup-database-approve <profile> --confirm <exact-generated-phrase>
hermes journal workload-check <date>
hermes journal workload-approve <date> --confirm <exact-generated-phrase>
hermes journal setup-plan --profile <name> --platform <name> --timezone <IANA> --pii-mode <mask|preserve> --entropy-mode <report|redact|off>
hermes journal setup-approve <plan-id> --confirm <exact-generated-phrase>
hermes journal preview <range>
hermes journal cron-setup
hermes journal cron-remove
hermes journal maintenance
hermes journal purge
hermes journal purge --apply --confirm "DELETE MY JOURNAL DATA"
```

`cron-setup` accepts recurring five field cron expressions or explicit interval schedules such as `every 1h`.
The native Windows bridge forwards setup and removal into the WSL ownership transaction. A fixed native worker exposes only create, list, and remove to the Windows Hermes cron API, while WSL retains the durable intent lock until the exact native job is verified and receipted. Bare intervals and one time schedules are rejected before native creation. It writes durable ownership intent before creating a native Hermes agent job with a required pre-run script, verifies the complete stored native shape, then reconciles that exact structured job during repeated setup, removal, and purge. The scheduler executes the trusted precollector before constructing its session database or agent, then runs synthesis through the normal restricted cron route.

The plugin deliberately does not register `/journal`. A plugin slash command bypasses the model. The separately installed `journal` skill owns `/journal`, calls these tools, and produces grounded semantic summaries.

## Configuration

The plugin reads:

* `MY_JOURNAL_ROOT`, defaulting to `$HERMES_HOME/journal`
* `MY_JOURNAL_VALIDATOR`, defaulting to `$HERMES_HOME/skills/note-taking/my-journal/scripts/validate_journal.py`
* `MY_JOURNAL_TIMEZONE`, overriding `config.json` in the journal root
* `config.json` key `timezone`, defaulting to the host local timezone when absent

The `0.2.0` release declares Linux, macOS, and native Windows Hermes support when Ubuntu WSL provides the restricted Journal runtime. Native Windows operation without Ubuntu WSL is not claimed.

## Safety contract

`journal_read_entries` returns note content only after the evidence manifest, digest bindings, exact session coverage, provenance, and secret checks pass. Validation is in memory and does not write completion state.

Generation collects once or resumes the same immutable packet plan, requires a bound digest receipt for every chunk, and publishes only after full evidence validation. If final publication fails, the previous canonical note and completion state are restored together.

Validated note text is capped at 200,000 characters per request. Accepted and rejected record metadata, status entries, and calendar gap lists are independently capped and report when truncation occurred.

`journal_recurring_issues` and `journal_improvement_report` read only fully validated entries, list unvalidated or tampered dates as excluded, and never write. `journal_lesson_record` is the only writer among them: it appends one adopt or retire event to `improvements/lessons.jsonl` through descriptor anchored atomic replacement under a directory lock, refuses duplicates, future or invalid dates (dates must be strict `YYYY-MM-DD`), malformed or oversized input (including non list `source_dates` and titles or terms with control or bidirectional format characters), and symlinked or non regular ledger paths such as FIFOs, which are refused without blocking, and fails closed on a malformed ledger. A durable append whose verification read differs is reported as `ok` with a `warning`. Recurring issue grouping processes at most 5,000 items per call and lists at most 31 excluded dates with bounded errors, each with a truncation flag. Improvement verdicts are `insufficient evidence` (fewer than 7 validated dates in a window), `no baseline occurrences` (nothing matched before adoption, so improvement cannot be assessed), `no recurrence observed since adoption`, `reduced`, or `not improved`. None of these tools belong to the `my-journal-generation` toolset. Purge removes the `improvements` directory with other journal data.

Journal notes, evidence manifests, and digests must remain inside their configured journal root. Symlinked notes that resolve outside the notes directory are ignored.

## Honest limitations

A calendar gap means a daily journal note is missing. It does not prove that Hermes conversations exist on that date.

A backfill plan is read only. Executable backfill is a separate command and starts only after explicit invocation.

Guided first use inventories profile and platform labels without message bodies, then requires explicit allowlists and privacy choices. Its immutable plan discovers eligible activity dates from retained conversation timestamps. Persistent memory is not a chronological source and does not choose the start date.

The normal database tier is 8 GiB. Larger databases require an explicit profile bound 16 GiB or 32 GiB approval before SQLite opens. The immutable setup plan selects the smallest sufficient daily message tier from 25,000, 50,000, and 100,000. If a later date exceeds its approved tier, collection stops before launch and returns the exact next targeted approval phrase. The triggering date authorizes a global reusable daily tier; targeted means no scope, privacy, database, or unrelated resource limit changes. Neither boundary increases silently.

Recurring issue grouping is lexical. Paraphrases with few shared words may stay separate, and unrelated items that share vocabulary may merge. Improvement verdicts compare recorded frequencies and cannot establish cause.

Evidence validation proves source coverage and provenance. It cannot guarantee that every sentence in a model written summary is the best possible interpretation.

The journal reflects retained Hermes databases. Deleted or unavailable source history cannot be reconstructed.

Large historical ranges can require substantial model time and tokens after entries are generated.
