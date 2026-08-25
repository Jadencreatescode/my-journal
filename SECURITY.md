# Security Policy

## Supported version

Versions `0.1.0` and `0.2.0` receive security fixes.

## Reporting

Report suspected vulnerabilities through [GitHub private vulnerability reporting](https://github.com/Jadencreatescode/my-journal/security/advisories/new) before opening a public issue. Include affected version, operating system, reproduction steps, and whether private journal data may have been exposed. Do not include real credentials, session databases, or private journal content.

The maintainer will acknowledge a complete report when practical, investigate impact, and coordinate disclosure after a fix or documented mitigation exists.

## Security boundaries

My Journal treats the Hermes home, journal root, package source, validator, SQLite databases, evidence, digests, completion receipts, canonical notes, automation ownership, and lifecycle metadata as trust boundaries. Descriptor anchored operations reduce symlink and path replacement races. Completion evidence binds content hashes and filesystem identities, then rechecks every live artifact before success. Lifecycle metadata uses bounded no follow reads and durable atomic replacement. Install, restore, uninstall, cron ownership, and purge use advisory locking and recoverable state. SQLite file size and raw field ceilings apply before redaction, followed by collection and output ceilings. Validation fails closed.

Redaction is defense in depth. It is not a guarantee that arbitrary sensitive content cannot appear in evidence or model output.
