---
name: my-journal
description: Use when creating, updating, reviewing, or automating a dated progression journal from Hermes conversations across every platform and profile. Produces evidence backed daily work records, project progression, decisions, verified changes, blockers, open threads, and automation activity without treating the journal as prompt memory.
version: 0.1.0-alpha.1
author: Jaden Gibson
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [journal, timeline, sessions, progression, daily-notes, audit]
    related_skills: [obsidian, knowledge-stack]
---

# My Journal

## Overview

My Journal turns Hermes session history into a dated progression record. It is neutral and shareable. It works for personal, professional, and mixed conversations, but its default voice is a factual work log rather than an intimate life diary.

The journal is separate from Hermes prompt memory. Raw sessions remain the source record. Journal notes explain what happened over time. Only durable facts should later be promoted into memory, and reusable procedures belong in skills.

## When to Use

Use this skill when the user asks to:

1. Create or update a daily Hermes journal.
2. Review progress across days, projects, profiles, or platforms.
3. Find when a decision or change occurred.
4. Generate weekly, monthly, or yearly progression summaries.
5. Install or repair the journal automation.
6. Mark an interaction as important, private, professional, personal, or mixed.

Do not use it to replace exact session search, permanent memory, project documentation, or a raw compliance archive.

## Core Guarantees

1. Include every discovered Hermes platform and profile by default.
2. Never exclude material merely because it is personal, professional, or mixed.
3. Context labels classify presentation only. They are not filters unless the user explicitly changes scope.
4. Distinguish discussed, decided, attempted, completed, verified, blocked, and corrected states.
5. Preserve provenance through source profile, platform, session ID, message IDs, and timestamps.
6. Exclude hidden reasoning and secret values from journal output.
7. Never claim a change was completed unless evidence records a successful result or a later verification.
8. Preserve historical corrections as append only amendments rather than silently rewriting old claims.
9. Keep raw evidence separate from the readable journal.
10. Do not inject the whole journal into future prompts. Retrieve only relevant entries when needed.

## Architecture

The standard system has four layers:

1. **Session source:** Read only SQLite databases under the active Hermes home and its profile directories.
2. **Collector:** `scripts/collect_journal.py` discovers databases, gathers new records, redacts likely secrets, and writes a complete evidence manifest plus a bounded model packet.
3. **Writer:** A scheduled Hermes agent reads the packet, follows this skill, and writes the daily Markdown entry.
4. **Knowledge layer:** Store the readable note in the user's chosen Markdown or Obsidian journal. Semantic indexing is optional and must not replace the canonical note.

## Standard Workflow

### Step 1: Resolve the date window

Use the user's configured timezone. A daily run normally covers the previous local calendar day. Record the exact UTC start and end in the evidence manifest.

Completion criterion: the run has one unambiguous local date and a half open UTC time range.

### Step 2: Collect all session databases

Run the collector with the active Hermes home. It discovers:

1. The default `state.db`.
2. Every `profiles/*/state.db` database.
3. Every source platform represented in those sessions, including CLI, Discord, Telegram, Slack, WhatsApp, Signal, SMS, email, API, cron, and future adapters.

Do not maintain a hardcoded platform allowlist. Platform names are data.

Completion criterion: the collection report lists every readable database and explicitly lists any database that failed.

### Step 3: Build evidence

Retain enough information to account for every conversation while bounding model context:

1. Every session receives a coverage record.
2. Every user message receives a compact excerpt and message ID.
3. Final assistant responses receive compact excerpts.
4. Tool activity is reduced to named events, statuses, and short result evidence.
5. Large raw tool outputs are never copied into the model packet.
6. The complete bounded evidence manifest remains on disk even when the model packet is further compressed.

Completion criterion: coverage counts reconcile with selected database rows and no selected session disappears from the manifest.

### Step 4: Write the daily note

Use `templates/daily-entry.md`. Keep the voice neutral, chronological, and evidence aware. Include:

1. Overview
2. Conversation coverage
3. Projects and workstreams
4. Decisions
5. Changes and verification
6. Completed work
7. Blockers and failures
8. Corrections and preference changes
9. Open threads
10. Personal, professional, and mixed context index
11. Automation appendix
12. Provenance

If the evidence is too large for one pass, summarize session groups first, then synthesize those summaries. Each grouped digest must declare the exact current run ID and evidence SHA256, then copy every `Session Ref:` line for its sessions exactly once. Do not drop or duplicate sessions to fit context.

Completion criterion: every manifest session coverage reference appears exactly once across the declared digest directory and readable note.

### Step 5: Verify the note

After writing:

1. Read the exact note back.
2. Confirm the date and all required headings as exact lines.
3. Confirm the evidence SHA256, manifest path, digest directory, run ID, database status, counts, platforms, and profiles exactly match the manifest.
4. Confirm no supported secret pattern is present in the note or digest files.
5. Confirm every digest declares the current run ID and evidence SHA256.
6. Reject symlinked digest files and any digest child that resolves outside the declared digest directory.
7. Validate every documented top level, database, session, and message field with fail closed type checks.
8. Confirm every manifest session coverage reference appears exactly once across the note and declared digests.
9. Only then commit the validated completion state.

Completion criterion: validation passes, completion state is committed, and the canonical note is readable from its destination.

### Step 6: Roll up progression

Weekly summaries read daily notes, monthly summaries read weekly notes, and yearly summaries read monthly notes. They should not reread the entire raw session database unless repairing missing history.

Completion criterion: each rollup cites its source notes and preserves unresolved open threads.

## Context Classification

Use one of four labels:

1. `professional`
2. `personal`
3. `mixed`
4. `unclear`

Classification changes organization, never inclusion inside the profiles and platforms that the user explicitly allowed. Do not infer sensitive personal attributes. When uncertain, use `unclear`.

## Evidence States

Use these states consistently:

1. `discussed`: an idea was explored.
2. `decided`: the user selected a direction.
3. `attempted`: an action began.
4. `completed`: the action reported success.
5. `verified`: a separate check confirmed the result.
6. `blocked`: an obstacle prevented completion.
7. `corrected`: a later fact amended an earlier record.

## Privacy and Security

1. All platforms and authorized group conversations are included by default because that is the journal contract.
2. Do not reproduce credentials, tokens, passwords, private keys, authorization headers, or raw environment files.
3. Redaction is defense in depth, not proof that source data is safe. Keep evidence files private.
4. Do not publish the user's journal merely because the skill itself is shareable.
5. Shared skill code and templates must contain no user specific paths, identifiers, channels, credentials, or project names.
6. Journal data belongs in a private destination chosen during installation.

## Configuration

Copy `templates/config.json` to a private runtime directory and edit values there. Never put private journal data or installation specific configuration inside the shareable skill directory.

Important defaults:

1. Collection is disabled until explicit consent is recorded.
2. Profiles and platforms require explicit nonempty allowlists.
3. Personal, professional, mixed, and unclear entries inside that scope are included.
4. Internal reasoning is excluded.
5. Secret redaction is mandatory.
6. Email and phone masking is enabled by the shipped template.

## Scheduling

A skill alone does not run itself. Install one daily agent cron job that:

1. Runs the collector script.
2. Reads the generated model packet.
3. Writes and verifies the daily note.
4. Runs the journal validator.
5. Commits completion state only after validation.
6. Delivers a concise success or blocker report.

Use `templates/cron-prompt.md` as the self contained scheduled task prompt.

## Common Pitfalls

1. **Treating the journal as memory.** This increases prompt cost and destroys chronology. Retrieve journal entries on demand instead.
2. **Reading entire tool outputs.** Tool payloads dominate session storage. Reduce them deterministically before model use.
3. **Silently omitting profiles.** Discover profile databases every run.
4. **Hardcoding platform names.** Store whatever source value Hermes records.
5. **Equating tool success with verification.** A successful write is completed; a readback or health check makes it verified.
6. **Filtering personal content by default.** Classification is not exclusion when scope is all.
7. **Committing completion before note validation.** This can mark incomplete or broken entries as successful.
8. **Writing user paths into the shared skill.** Keep runtime configuration private and external.
9. **Letting summaries invent motives or emotions.** Record only what the user stated or what evidence supports.
10. **Overwriting historical errors.** Append corrections and retain the original dated record.

## Verification Checklist

1. Every discovered readable database appears in the run report.
2. Every selected session appears in the evidence manifest.
3. Every source platform remains represented.
4. Personal, professional, mixed, and unclear classifications are all included.
5. No hidden reasoning is copied.
6. Tool outputs are bounded.
7. Likely secrets are redacted.
8. Required daily headings exist.
9. Evidence and note dates agree.
10. Provenance exactly includes run ID, evidence SHA256, manifest path, digest directory, database status, counts, platforms, and profiles.
11. Every session coverage reference appears exactly once across the readable note and declared digests.
12. The canonical note was read back.
13. Completion state was committed only after validation.
