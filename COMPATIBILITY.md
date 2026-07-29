# Compatibility

## Supported

1. Linux and macOS.
2. Python 3.11, 3.12, and 3.13.
3. Hermes Agent installations that expose standalone plugins, skills, native cron management, and the noninteractive `hermes chat` command.
4. IANA timezones available through Python `zoneinfo`.
5. Hermes SQLite session databases containing the supported sessions and messages columns. Optional legacy columns may be absent.

## Not claimed

1. Native Windows support.
2. Complete recovery of deleted sessions.
3. Compatibility with arbitrary third party journal schemas.
4. A stable API before the alpha series is complete.
