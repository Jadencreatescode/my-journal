---
name: journal
description: Query validated My Journal entries by date, project, change, decision, timeline, or open work.
version: 0.2.0-alpha.1
author: Jaden Gibson
license: MIT
metadata:
  hermes:
    tags: [journal, timeline, summaries, decisions, progression]
    related_skills: [my-journal]
    requires_toolsets: [journal]
---

# Journal Command Interface

## Overview

Use this skill when the user invokes `/journal` or asks what happened, changed, was decided, failed, completed, corrected, or remains open across a period of Hermes activity.

This is the conversational layer. The journal plugin owns deterministic date resolution, status, gaps, backfill planning, and evidence validated entry reads. The `my-journal` skill owns collection and completion validation.

## Command Routing

### Help

For `/journal` or `/journal help`, show concise examples for:

```text
/journal summarize last 7 days
/journal changes last 30 days
/journal timeline 2026-07-01 to 2026-07-15
/journal project VTON Studio last 90 days
/journal decisions last month
/journal open last 30 days
/journal search laptop tunnel last 90 days
/journal compare last week with this week
/journal status
/journal gaps last 90 days
/journal backfill plan last 30 days
/journal generate 2026-07-27
/journal backfill last 30 days
/journal setup
```

### Status

For `/journal status`, call `journal_status`. Report the journal root, entry count, earliest and latest entries, and gaps. Do not imply that calendar gaps contain Hermes activity unless source evidence has been checked.

### Guided first use

For `/journal setup`:

1. Call `journal_setup_inventory`. Explain that this user initiated metadata inventory lists available profile and platform labels without reading message bodies. If `blocked_databases` requests a 16 GiB or 32 GiB tier, show the profile, size, required tier, and exact phrase. Call `journal_setup_database_approve` only after the user repeats that phrase, then repeat inventory. A database above 32 GiB remains blocked.
2. Ask the user to choose explicit nonempty profile and platform allowlists from that inventory.
3. Ask for an IANA timezone, `pii_mode` (`mask` or `preserve`), `entropy_mode` (`report`, `redact`, or `off`), optional excluded sessions, and optional start or end overrides.
4. Call `journal_setup_plan` with every explicit choice. The plan automatically discovers the earliest and latest eligible retained conversation activity dates after scope, exclusion, role, platform, profile, and timezone rules. Persistent memory must not determine the start date.
5. Preview the exact scope, date bounds, activity dates, existing dates, missing dates, total message metadata workload, maximum daily message count, selected 25,000, 50,000, or 100,000 tier, and generated confirmation phrase. Do not enable collection or generate anything yet.
6. Require the user to repeat the exact confirmation phrase. General agreement such as “yes” is insufficient.
7. Call `journal_setup_approve` with the exact plan identifier and phrase. It rechecks the retained source fingerprint, enables the approved configuration, and processes only missing activity dates oldest first through the restricted generation route.
8. Report completed and failed dates separately. A partial result is not success. Repeating approval retries only unfinished dates.
9. Offer native daily scheduling only when approval reports total success. Do not schedule automatically.

### Summaries and semantic views

For `summarize`, `changes`, `timeline`, `project`, `decisions`, `open`, `search`, or `compare`:

1. Extract the requested date phrase. If absent, use `last 7 days` and state that default.
2. Call `journal_resolve_range` when the boundaries need confirmation.
3. Call `journal_read_entries` for the resolved range.
4. Stop if the tool returns an error.
5. If any entries are rejected, name those dates and validation errors before summarizing accepted entries.
6. Ground every claim in the returned validated notes. Never supplement missing dates with assumptions.
7. Shape the result by command:

   `summarize` reports what happened, what changed, decisions, completed work, verification, failures, corrections, and open threads.

   `changes` reports meaningful before and after states only.

   `timeline` reports a dated chronological sequence.

   `project` follows one named project from goal through current state.

   `decisions` separates proposed, confirmed, superseded, corrected, and unimplemented decisions.

   `open` reports unresolved blockers, failed attempts, pending decisions, unverified changes, and genuinely open threads. Do not revive stale requests merely because they were once mentioned.

   `search` returns matching dates and grounded excerpts or concise context.

   `compare` reads both periods separately and reports new work, completed work, changed priorities, resolved blockers, new blockers, and changed decisions.

### Gaps and backfill planning

For `/journal gaps <range>`, call `journal_find_gaps`. Explain that these are missing journal note dates, not automatically confirmed missing conversations.

For `/journal backfill plan <range>`, call `journal_plan_backfill`. This is read only. Report existing and missing note counts and the exact candidate dates. Do not start collection without a separate explicit instruction.

### Generation and executable backfill

For `/journal generate <date>`, invoke the scriptable `hermes journal generate <date>` route. That route creates a child Hermes process pinned to the `my-journal-generation` toolset and the `my-journal` skill. Do not attempt generation in the conversational journal agent, do not expose packet text to its broader retrieval tools, and do not count the child response as success unless the canonical date passes `validated_entry_dates`.

If generation reports `daily_workload_approval_required`, show the actual count, approved tier, required tier, and exact phrase. Call `journal_daily_workload_approve` only after the user repeats that phrase, then retry the same date. The triggering date raises the global reusable daily tier for future dates. Targeted means only daily capacity changes; it is not permission to change profiles, platforms, exclusions, timezone, privacy, database limits, or other ceilings. If the date exceeds 100,000 eligible messages, stop and explain that a separately reviewed bounded strategy is required.

For `/journal backfill <range>`, invoke the scriptable `hermes journal backfill <range>` route. It resolves the bounded missing-date plan and launches one independently validated restricted generation run per missing day, oldest first. Report completed and failed dates separately; partial success never upgrades the overall result to success.

Never recollect a date merely because digesting or synthesis was interrupted. Resume from its immutable packet plan and accepted digest receipts.

## Safety Rules

1. Read journal content only through `journal_read_entries` for semantic commands.
2. Never bypass a rejected entry by opening its Markdown directly.
3. Never reconstruct text marked redacted.
4. Keep personal and professional activity unless the user narrows scope.
5. Distinguish source absence, journal gaps, validation failures, and no meaningful changes.
6. Backfill planning never authorizes backfill execution.
7. Setup inventory never authorizes content collection. Setup planning never authorizes generation. Only exact plan approval does.

## Verification

Before answering, confirm that every summarized entry appears in the plugin response, every rejection is disclosed, the requested range is stated, and no unsupported date or project claim was added.
