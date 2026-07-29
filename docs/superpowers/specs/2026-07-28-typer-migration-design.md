# Migrate CLI parsing from argparse to Typer

## Problem

`playlist_updates.py`'s CLI parsing uses a top-level `argparse.REMAINDER` positional
alongside `add_subparsers` to pass unrecognized flags through to oauth2client's own
argparser. This causes any option that consumes a separate-token value (e.g.
`--since 2026-05-22`) to be misparsed as an invalid subcommand choice. The immediate
bug was fixed with `parse_known_args()`, but the user wants to migrate off argparse
entirely to a more modern, type-hint-driven CLI library.

## Scope

- Replace argparse with Typer for the two existing subcommands: `sort` and `update`.
- Drop the oauth2client unknown-args passthrough entirely (confirmed unused in
  practice) rather than replicating it with Click's `ctx.args` mechanism.
- Idiomatic Typer conventions are acceptable even where they change the CLI surface
  slightly (e.g. `--dry-run` moving from per-subcommand to a shared global option
  before the subcommand name). Not required to preserve 100% invocation-identical
  behavior.
- No test suite exists for this project (`make test` is a no-op); verification is
  manual.

## Design

### Dependency

Add `typer` via `uv add typer` (pulls in `click` transitively). New runtime
dependency — approved by user for this migration.

### Command structure

Single `typer.Typer()` app instance. `sort` and `update` become `@app.command()`
functions. `--dry-run` is defined once on a shared `@app.callback()` and stored on
`typer.Context`; each command function reads it from context. This makes `--dry-run`
a global option preceding the subcommand:

```
playlist_updates.py --dry-run update -f --since 2026-05-22 --auto-batch
```

### Options

- `update` command:
  - `--since`: parsed via `arrow.get` (same conversion as today).
  - `--until`: parsed via `arrow.get`.
  - `--auto-batch`: boolean flag.
  - `--until` and `--auto-batch` are mutually exclusive. Typer/Click has no built-in
    mutually-exclusive-group construct (unlike argparse). Enforced manually in the
    command body: `if until and auto_batch: raise typer.BadParameter(...)`. This was
    chosen over a Click parameter callback (unnecessary indirection for a two-option
    check) and over a third-party library like `click-option-group` (a whole new
    dependency for something one `if` statement handles).
  - `-f` / `--only-allowed`: boolean flag, same meaning as today.
- `sort` command: only the shared `--dry-run`, no other options.
- `--help` output is Typer/Click-generated from type hints and docstrings; wording
  and formatting differences from the current argparse help text are expected and
  acceptable.

### Credentials / oauth2client

`get_creds()` no longer accepts a CLI args parameter. It calls
`oauth2client.tools.argparser.parse_args([])` internally to obtain oauth2client's own
default flags (`auth_host_name`, `auth_host_port`, `logging_level`,
`noauth_local_webserver`, etc.) needed by `oauth2client.tools.run_flow`.
`YoutubeManager.__init__` drops its `args: List[str]` parameter accordingly, since
nothing upstream needs to supply it anymore.

### Testing / verification plan

No automated test suite exists for this project. Verification is manual, matching
the bar already used for the recent bug fixes in this file:

- `--help` on both `sort` and `update` produce sensible output.
- `--dry-run update` with combinations of `--since`, `--until`, `--auto-batch`, `-f`
  behaves as before.
- `--until` and `--auto-batch` together produce the expected `BadParameter` error.
- `make check` (ruff + mypy) passes.

## Out of scope

- Splitting `playlist_updates.py` into multiple files.
- Adding automated tests (none exist today; not part of this migration).
- Changing any behavior other than the CLI parsing layer and the oauth2client
  passthrough removal described above.
