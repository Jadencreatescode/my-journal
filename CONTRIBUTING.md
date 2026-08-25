# Contributing

## Development

Use Python 3.11 through 3.13 on Linux or macOS. The project uses only the Python standard library at runtime.

Run the release hygiene checks before the suites:

```text
python3 scripts/check_release_tree.py
python3 scripts/check_public_content.py
python3 scripts/compile_all.py
```

Run all suites separately:

```text
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s plugins/my-journal/tests -v
python3 -m unittest discover -s skills/note-taking/my-journal/tests -v
```

Security, privacy, installer, and release changes require a failing regression before production code changes.

The committed social preview is a release asset. Rebuilding it with `scripts/build_social_preview.py` requires Pillow as an optional authoring dependency. Pillow is not imported by the My Journal runtime, installer, plugin, skills, or synthetic demonstration.

## Fixture privacy

Use synthetic identifiers, messages, paths, channels, and tokens. Never commit a real journal, SQLite database, credential, evidence packet, digest, account identifier, or private conversation.

## Contributions

By submitting a contribution, you certify that you have the right to provide it under the MIT License. Public security reports should follow `SECURITY.md`.
