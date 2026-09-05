# youtube-sort-playlist

CLI tool that adds recent uploads from allowlisted YouTube subscriptions to a
"Sort Watch Later" playlist and sorts that playlist by channel + publish date.
Single-file app: `playlist_updates.py` (Typer CLI wrapping `YoutubeManager`).

## Commands

```bash
make venv       # uv sync (dev group, --no-install-project) + install pre-commit hooks
make fix        # pre-commit run --all-files (ruff check --fix, ruff format, hygiene hooks)
make lint       # ruff check . (no edits)
make typecheck  # mypy playlist_updates.py
make check      # lint + typecheck, non-mutating
make update     # venv, then: uv run playlist_updates.py update --auto-batch
make sort       # venv, then: uv run playlist_updates.py sort
make update-lock  # uv lock
make clean      # rm -rf .venv venv
```

`update` and `sort` both depend on `venv`, so every invocation re-syncs the env
and re-runs `pre-commit install` first.

See README.md for the full set of direct `uv run playlist_updates.py ...`
invocations (`--since`, `--until`, `--dry-run`, `subscriptions add/list/remove`).
Note: `--dry-run` is a top-level option (defined on the Typer callback), so it
must precede the subcommand: `playlist_updates.py --dry-run update`, not
`update --dry-run`.

No test suite exists — `make test` is an intentional no-op placeholder.

## Architecture

- `playlist_updates.py` — everything lives here: `YoutubeManager` (all YouTube
  Data API v3 calls + config I/O) and a Typer `app` with `sort`, `update`, and
  a `subscriptions` sub-app (`add`/`list`/`remove`).
- Config/state: `$XDG_CACHE_HOME/youtube-sort-playlist/config.yaml`, holding
  `auto_add` (allowlisted channels) and `last_updated` (watermark timestamp).
- Auth: `client_secrets.json` (OAuth app, project root, gitignored) +
  `{argv[0]}-oauth2.json` (cached user token, gitignored).
- `docs/superpowers/{plans,specs}/` — design docs from past feature work
  (typer migration, subscriptions subcommand); check before large changes to
  see if a similar change was already scoped.
- `opencode.jsonc` — opencode-specific agent permissions; requires approval
  to read `*.env`, `*oauth2.json`, `client_secrets.json`. A second, separate
  agent-config file from this one, relevant to any agent (not just Claude
  Code) working in this repo.

## Gotchas

- **`update` only touches allowlisted channels**, but even with an *empty*
  allowlist it still advances `last_updated` to now (it only skips
  fetch/insert, not the watermark write). `subscriptions add` must be run
  first (interactive fuzzy multi-select) — otherwise later-added channels
  silently miss everything published before the empty run.
- **`--until` and `--auto-batch` are mutually exclusive** (raises
  `BadParameter`): `--until` sets a fixed cutoff, `--auto-batch` computes its
  own cutoff from the quota cap below.
- **`self.youtube` is thread-local**, not a plain cached property.
  `httplib2.Http` isn't thread-safe (shared connection cache), and channel
  fetches run concurrently via `asyncio.to_thread`; sharing one client across
  threads causes hangs or heap corruption. Don't refactor this into a normal
  `cached_property`.
- **OAuth is lazy** (`_credentials` is a `cached_property`, not eager in
  `__init__`) so that `subscriptions list`/`remove` — local-file-only — never
  trigger a browser OAuth flow.
- **Batch failures are all-or-nothing by design**, not a bug to "fix" with
  partial retry: `fetch_all_channels_videos` aborts the whole fetch on any
  channel error, and `insert_videos_watch_later` aborts the whole insert on
  any hard failure. This guarantees `last_updated` is never advanced past
  videos the run never actually saw/inserted.
- **Quota-aware batching**: `MAX_INSERTS_PER_RUN = 160` (80% of the 10k daily
  quota / 50 cost per insert). `update --auto-batch` caps a run there by
  setting `last_updated` to the `published_at` of the first video *not*
  queued, not "now" — the cutoff is a timestamp, not a count, so ties at the
  boundary can queue fewer than 160. The remainder is picked up next run.
- **Inserts are serial, not concurrent** (unlike fetches): concurrent writes
  to the same playlist can trigger spurious 409s that look identical to a
  real duplicate, which the 409-skip handling can't tell apart. Insert order
  doesn't matter for correctness — `sort` sets final position separately.
- `print` is rebound to `tqdm.write` at module level so plain `print()` calls
  don't corrupt progress bars — don't reassign or shadow this.
- `'Sort Watch Later'` is a **regular user-created playlist**, distinct from
  YouTube's built-in "Watch Later" — the code asserts this distinction, it
  does not attempt to touch the built-in one.
- `read_config()` and `get_watchlater_playlist()` are both `@lru_cache(1)`:
  config is read from disk once per process and the in-memory dict is
  mutated in place thereafter (`write_config` doesn't invalidate the cache,
  it doesn't need to — same dict object). The `if not watchlater_id` guard in
  `sort()` is dead code: the lookup raises `StopIteration` before it could
  ever return falsy.

## Code style

- Ruff: line length 120, `quote-style = "preserve"` (don't normalize quotes
  in diffs), lint rules `E, F, I, T10, W`.
- mypy: `ignore_missing_imports = true` (many deps here are untyped).
- Python `>=3.11` per `pyproject.toml` (README says `~=3.9`; trust
  `pyproject.toml` — that's what `uv` actually resolves against). Existing
  code still uses `typing.Dict/List/Optional` rather than builtin generics —
  match that in new code rather than mixing styles.
