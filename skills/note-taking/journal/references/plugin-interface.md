# Hermes Plugin and Conversational Interface

## Hybrid architecture

1. The evidence core collects, redacts, hashes, and validates journal runs.
2. A Hermes plugin exposes deterministic read tools for date resolution, status, validated entry reads, note gaps, and read only backfill planning under `journal`, plus a separate `my-journal-generation` toolset containing only collect, immutable chunk retrieval, digest receipt, and validated atomic publication operations.
3. The `journal` skill owns the conversational `/journal` interface and turns validated entries into summaries, timelines, decisions, project histories, and open thread reports.
4. The plugin may register a scriptable `hermes journal` CLI tree for automation and operators.

## Critical dispatch rule

Do not register `/journal` through `PluginContext.register_command()` when semantic synthesis is required. Plugin slash commands are handled locally and bypass the language model. A registered plugin command would shadow the model invoked journal skill.

Instead, let the skill provide `/journal`, register deterministic model tools through the plugin, and register `hermes journal` for scripting.

## Safe retrieval contract

A journal read tool should:

1. Resolve a bounded date range in the configured timezone.
2. Discover only date named note files inside the configured notes directory.
3. Reject symlinks or resolved paths outside that directory.
4. Parse unique provenance lines for manifest and digest paths.
5. Require those paths to remain inside configured evidence and runs directories.
6. Invoke manifest, digest binding, and note validation in memory.
7. Return content only for entries that pass every check.
8. Report rejected dates and errors instead of silently omitting them.
9. Enforce an output character limit.
10. Never write completion state during a read operation.

## Status and backfill language

A calendar gap means a daily journal note is missing. It does not prove that source conversations exist for that date.

An initial backfill plan may list missing note dates, but must not claim source activity until it inspects source databases. Planning does not authorize collection or generation.

## Destination verification

When an indexing system normalizes Markdown, keep one byte exact canonical destination and treat the index as a semantic mirror. Verify semantic mirrors by removing known destination metadata and normalizing only documented boundary whitespace. Record the raw mirror hash separately. Do not weaken the canonical exact hash requirement.

## Release checks

1. Test the plugin core and evidence core separately.
2. Exercise live plugin discovery and enablement.
3. Exercise the real CLI command tree.
4. Exercise one live validated entry read.
5. Confirm the plugin does not register a slash command that shadows the skill.
6. Restart the gateway only after independent review, because a restart can terminate in flight reviewer processes.
