# Privacy

## Data read

My Journal reads only configured Hermes `state.db` files from explicitly allowed profiles and platforms. Collection is disabled by default. Excluded session identifiers are filtered before aggregate accounting.

## Data produced

Private runtime data can include evidence manifests, packet chunks, digest receipts, generated Markdown notes, validation state, generation receipts, installer backups, and durable cron ownership intent and receipts. These remain under the selected Hermes and journal roots.

## Provider exposure

Evidence chunks and digests used for generation can be sent to the model provider configured in Hermes. Users should review their provider terms and data settings before enabling collection.

Unattended generation runs with the dedicated `my-journal-generation` toolset. That toolset can collect one configured date, retrieve bounded immutable chunks, record bound digest receipts, and publish only through full validation. It does not expose general Hermes tools.

## Public provenance policy

The stable release uses nonreversible coverage references. Raw session identifiers, chat identifiers, thread identifiers, and absolute database paths are not published in evidence artifacts. No reversible private identifier map is created.

## Redaction limits

Known credential formats are redacted. Email and phone data are masked under the shipped policy. Entropy based detection can report or redact suspicious opaque values. These controls reduce risk but cannot prove complete removal.

## Deletion

`hermes journal purge` previews owned journal data, automation ownership records, and strict crash residue patterns. Applying purge requires the exact confirmation phrase, reconciles and disables every exact owned Hermes cron job before deletion, removes generation receipts, and preserves `config.json`. If cron removal fails, journal data is preserved. Uninstall removes installed code while preserving journal data. Session databases are never purge targets.
