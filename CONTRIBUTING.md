# Contributing

## Development

Use Python 3.11 through 3.13 on Linux or macOS. The project uses only the Python standard library at runtime.

Run all suites separately:

```text
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s plugins/my-journal/tests -v
python3 -m unittest discover -s skills/note-taking/my-journal/tests -v
```

Security, privacy, installer, and release changes require a failing regression before production code changes.

## Fixture privacy

Use synthetic identifiers, messages, paths, channels, and tokens. Never commit a real journal, SQLite database, credential, evidence packet, digest, account identifier, or private conversation.

## Contributions

By submitting a contribution, you certify that you have the right to provide it under the MIT License. Public security reports should follow `SECURITY.md`.
