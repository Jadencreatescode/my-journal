# Security Policy

## Supported version

Only `0.1.0-alpha.1` receives security fixes during the alpha.

## Reporting

Report suspected vulnerabilities privately to the repository owner before opening a public issue. Include affected version, operating system, reproduction steps, and whether private journal data may have been exposed. Do not include real credentials, session databases, or private journal content.

The maintainer will acknowledge a complete report when practical, investigate impact, and coordinate disclosure after a fix or documented mitigation exists.

## Security boundaries

My Journal treats the Hermes home, journal root, package source, validator, SQLite databases, evidence, digests, and canonical notes as trust boundaries. Descriptor anchored operations reduce symlink and path replacement races. Collection limits reduce denial of service exposure. Validation fails closed.

Redaction is defense in depth. It is not a guarantee that arbitrary sensitive content cannot appear in evidence or model output.
