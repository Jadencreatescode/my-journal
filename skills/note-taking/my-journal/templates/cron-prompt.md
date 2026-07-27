You are creating the standard daily My Journal entry from Hermes progression data.

Load and follow the `my-journal` skill.

1. Determine the previous calendar date in the configured journal timezone.
2. Run `scripts/collect_journal.py` from the loaded skill with the active Hermes home, private journal output directory, date, and timezone.
3. Parse its JSON response. If any database failed, stop and report the exact database and error. Do not write a partial journal unless the runtime configuration explicitly permits it.
4. Read the generated model packet. If it is too large for one pass, summarize session groups in bounded chunks and save those intermediate summaries under a private digest directory for the run. Every digest must contain exact `Digest Run ID:` and `Digest Evidence SHA256:` lines for the current manifest. Copy every packet line beginning `Session Ref:` into exactly one digest. Every manifest session must remain represented.
5. Write the daily note using `templates/daily-entry.md`. Keep the voice neutral and focused on work, decisions, changes, verification, blockers, corrections, open threads, and Hermes progression. Include the exact evidence hash, manifest path, digest directory, database error count, platforms, profiles, and aggregate counts in provenance.
6. Include all personal, professional, mixed, and unclear content. Labels organize the note and never filter content under scope `all`.
7. Include meaningful automation in the appendix while collapsing routine healthy monitor noise.
8. Never reconstruct text marked REDACTED. Never copy hidden reasoning or raw large tool payloads.
9. Write the note to the configured canonical destination.
10. Read the exact note back and verify it exists.
11. Run `scripts/validate_journal.py` with the manifest path, final note path, completion state path, and digest directory.
12. If validation fails, stop and report every validation error. Do not mark the run complete.
13. If validation succeeds, deliver a concise report containing the journal date, note path, session count, message count, platforms, profiles, and verification status.
