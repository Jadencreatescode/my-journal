# My Journal Plugin

My Journal adds deterministic journal operations to Hermes while leaving semantic summaries to the conversational `journal` skill.

## What it provides

The plugin registers these model tools:

* `journal_status`
* `journal_resolve_range`
* `journal_read_entries`
* `journal_find_gaps`
* `journal_plan_backfill`

Read tools use the `journal` toolset. Unattended generation uses a separate `my-journal-generation` toolset containing only:

* `journal_generation_collect`
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
hermes journal generate <date>
hermes journal backfill <range>
hermes journal preview <range>
hermes journal cron-setup
hermes journal cron-remove
hermes journal maintenance
hermes journal purge
hermes journal purge --apply --confirm "DELETE MY JOURNAL DATA"
```

`cron-setup` accepts recurring five field cron expressions or interval schedules. It writes durable ownership intent before creating the native Hermes job, then reconciles that exact structured job during repeated setup, removal, and purge.

The plugin deliberately does not register `/journal`. A plugin slash command bypasses the model. The separately installed `journal` skill owns `/journal`, calls these tools, and produces grounded semantic summaries.

## Configuration

The plugin reads:

* `MY_JOURNAL_ROOT`, defaulting to `$HERMES_HOME/journal`
* `MY_JOURNAL_VALIDATOR`, defaulting to `$HERMES_HOME/skills/note-taking/my-journal/scripts/validate_journal.py`
* `MY_JOURNAL_TIMEZONE`, overriding `config.json` in the journal root
* `config.json` key `timezone`, defaulting to the host local timezone when absent

The standard release declares Linux and macOS support. Native Windows support requires a verified timezone data dependency and is not claimed in version 1.

## Safety contract

`journal_read_entries` returns note content only after the evidence manifest, digest bindings, exact session coverage, provenance, and secret checks pass. Validation is in memory and does not write completion state.

Generation collects once or resumes the same immutable packet plan, requires a bound digest receipt for every chunk, and publishes only after full evidence validation. If final publication fails, the previous canonical note and completion state are restored together.

Validated note text is capped at 200,000 characters per request. Accepted and rejected record metadata, status entries, and calendar gap lists are independently capped and report when truncation occurred.

Journal notes, evidence manifests, and digests must remain inside their configured journal root. Symlinked notes that resolve outside the notes directory are ignored.

## Honest limitations

A calendar gap means a daily journal note is missing. It does not prove that Hermes conversations exist on that date.

A backfill plan is read only. Executable backfill is a separate command and starts only after explicit invocation.

Evidence validation proves source coverage and provenance. It cannot guarantee that every sentence in a model written summary is the best possible interpretation.

The journal reflects retained Hermes databases. Deleted or unavailable source history cannot be reconstructed.

Large historical ranges can require substantial model time and tokens after entries are generated.
