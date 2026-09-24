# Compatibility

## Supported

1. Linux and macOS. Ubuntu under WSL is supported as a Linux environment.
2. Native Windows Hermes with Ubuntu WSL as a restricted Journal runtime. The native Hermes installation owns the plugin, skills, tools, and model interaction. WSL performs Journal filesystem operations and descriptor anchored SQLite reads. It does not run a second conversational Hermes agent.
3. Python 3.11, 3.12, and 3.13.
4. Hermes Agent installations that expose standalone plugins, skills, native cron management, and the noninteractive `hermes chat` command.
5. IANA timezones available through Python `zoneinfo`.
6. Hermes SQLite session databases containing the supported sessions and messages columns. Optional legacy columns may be absent.

## Not claimed

1. Native Windows operation without Ubuntu WSL.
2. Complete recovery of deleted sessions.
3. Compatibility with arbitrary third party journal schemas.
4. Compatibility beyond the documented `0.2.0-alpha.1` interfaces and file formats.
