# Add a `subscriptions` subcommand for managing auto-add channels

## Problem

`update`'s interactive add-prompt (approve each newly-seen subscribed channel via a
`y/n` prompt, unless `-f`/`--only-allowed` is passed) conflates two concerns: pulling
new videos, and managing which channels are allowed to auto-add videos. Running
`update -f` (the common case, e.g. from a cron job) silently skips onboarding new
channels — there's no way to add a channel without dropping into an interactive
`update` run. The user wants channel management split into its own subcommand, and
since a hand-editable YAML config already exists, wants the interactive parts (fuzzy
search, multi-select) to be pleasant rather than a typed-argument CLI.

## Scope

- New Typer sub-app `subscriptions` with three commands: `add`, `list`, `remove`.
- `update` loses its interactive add-prompt entirely, along with the now-dead
  `-f`/`--only-allowed` flag. `update` always restricts to channels already present
  in `config['auto_add']`.
- New dependencies: `rich` (table rendering for `list`) and `InquirerPy` (fuzzy
  multi-select prompts for `add`/`remove`) — approved by user for this feature.
- No test suite exists for this project; verification is manual (matches the
  precedent set in `docs/superpowers/specs/2026-07-28-typer-migration-design.md`).

## Design

### Command structure

```
playlist_updates.py subscriptions add
playlist_updates.py subscriptions list
playlist_updates.py subscriptions remove
```

Mounted via `app.add_typer(subscriptions_app, name='subscriptions')`. The existing
shared `--dry-run` global option (defined on the root `@app.callback()`) applies to
all three via `ctx.obj`, same mechanism `sort`/`update` already use.

### `YoutubeManager` methods

- `add_subscriptions() -> None`
  - `get_subscribed_channels()` to fetch current YouTube subscriptions.
  - Read config, `auto_add = config.setdefault('auto_add', [])`; compute known ids.
  - Candidates = subscribed channels whose id is not already in `auto_add`.
  - If no candidates: print `"No new channels to add."` and return.
  - Else: `InquirerPy.inquirer.fuzzy(message="Select channels to add:", choices=[...],
    multiselect=True).execute()`, one `Choice` per candidate (value = channel dict,
    name = channel title).
  - Append selected channels (`{'id':..., 'name':...}`) to `auto_add`.
  - `write_config(config)` unless `self.dry_run`.
  - Print summary of what was added (or "Nothing selected." if the multiselect
    returned empty).

- `list_subscriptions() -> None`
  - Read config's `auto_add`. If empty, print `"No subscriptions."`.
  - Else build a `rich.table.Table` (columns: Name, Channel ID), one row per entry,
    render via `rich.console.Console().print(table)`.

- `remove_subscription() -> None`
  - Read config's `auto_add`. If empty, print `"No subscriptions to remove."` and
    return.
  - `InquirerPy.inquirer.fuzzy(message="Select channels to remove:", choices=[...],
    multiselect=True).execute()`, one `Choice` per current entry.
  - Remove selected entries from `auto_add`.
  - `write_config(config)` unless `self.dry_run`.
  - Print summary of what was removed (or "Nothing selected." if empty).

### `update()` change

Delete the entire `if not only_allowed and not self.dry_run: ... write_config(config)`
prompt block and the `only_allowed` parameter. `update` always filters subscribed
channels down to those already in `config['auto_add']` — behavior identical to
today's `update -f`. The CLI's `-f`/`--only-allowed` option is removed from the
`update` Typer command since it would otherwise be a dead no-op flag.

### `--dry-run` semantics

Matches the existing convention (today's add-prompt block already gates its
`write_config` call behind `not dry_run`): `add`/`remove` still fetch data and run the
interactive prompt (so you can preview what you'd select), but skip `write_config` when
`--dry-run` is set, so nothing persists.

### Error handling

`InquirerPy`'s fuzzy prompt requires a real TTY. No special handling is added for
non-interactive environments — these are interactive-only commands, the same
assumption the current add-prompt already makes. This introduces no new failure mode
relative to existing code.

### Testing / verification plan

No automated test suite exists for this project. Verification is manual:

- `subscriptions add` with some subscribed channels not yet in `auto_add`: fuzzy
  multiselect appears, selecting some and confirming updates `config.yaml`.
- `subscriptions add` when all subscribed channels are already known: prints
  "No new channels to add." and does nothing.
- `subscriptions list` with a populated and with an empty `auto_add`.
- `subscriptions remove`: select existing entries, confirm they're gone from
  `config.yaml`.
- `--dry-run subscriptions add` / `--dry-run subscriptions remove`: prompt still
  appears, but `config.yaml` is unchanged afterward.
- `update` (no `-f` flag exists anymore) only pulls videos from channels already in
  `auto_add`; verify `--since`/`--until`/`--auto-batch` combinations still work as
  before.
- `--help` on `subscriptions`, `subscriptions add`, `subscriptions list`,
  `subscriptions remove` produce sensible output.
- `make check` (ruff + mypy) passes.

## Out of scope

- Any config format change beyond what `add`/`remove` already touch (`auto_add`
  list of `{'id', 'name'}` dicts — unchanged shape).
- Editing subscriptions any way other than through these three commands (e.g. a
  `rename` command) — direct YAML edits remain the fallback for anything not
  covered here, consistent with how the user already treats the config file.
- Automated tests (none exist today; not part of this feature).
