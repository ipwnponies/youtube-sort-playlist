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
make test       # pytest suite in tests/ (API mocked, no network)
make check      # lint + typecheck + test, non-mutating
make update     # uv run --locked playlist_updates.py update --auto-batch
make sort       # uv run --locked playlist_updates.py sort
make update-lock  # uv lock
make clean      # rm -rf .venv venv
```

`update` and `sort` run `uv run --locked`: uv syncs the env itself, and fails
if `uv.lock` is stale (run `make update-lock` after editing dependencies).
`make venv` is only needed once, for dev tools and the pre-commit hook.

See README.md for the full set of direct `uv run playlist_updates.py ...`
invocations (`--since`, `--until`, `--dry-run`, `subscriptions add/list/remove`).
Note: `--dry-run` is a top-level option (defined on the Typer callback), so it
must precede the subcommand: `playlist_updates.py --dry-run update`, not
`update --dry-run`.

Tests use pytest (fixtures, `parametrize`, `monkeypatch`, `tmp_path`) + `unittest.mock`.
They fake the API by setting `manager._thread_local.youtube` to a `MagicMock` and
patch module-level path constants / config functions so no real user file is touched.

## Architecture

- `playlist_updates.py` — everything lives here: `YoutubeManager` (all YouTube
  Data API v3 calls + config I/O) and a Typer `app` with `sort`, `update`, and
  a `subscriptions` sub-app (`add`/`list`/`remove`).
- Config: `$XDG_CONFIG_HOME/youtube-sort-playlist/config.yaml` (`auto_add`
  allowlist). State: `$XDG_STATE_HOME/youtube-sort-playlist/state.yaml`
  (`last_updated` watermark). The old combined file in `$XDG_CACHE_HOME` is
  migrated by the Typer callback (`migrate_legacy_config`) unless a new file
  exists; it is never deleted (a warning prints every run while it exists).
  Dry run never writes: reads fall back to the legacy file in memory.
- Auth: `client_secrets.json` (OAuth app, next to the script via `APP_DIR`,
  gitignored) + `$XDG_CONFIG_HOME/youtube-sort-playlist/token.json` (user token,
  atomic write, always forced 0600 even if the file on disk was more
  permissive), via google-auth / google-auth-oauthlib (`InstalledAppFlow`,
  port 8080). A corrupt/hand-edited `token.json` falls back to consent
  instead of crashing. `build(credentials=...)` per thread.
- `docs/superpowers/{plans,specs}/` — design docs from past feature work
  (typer migration, subscriptions subcommand); check before large changes to
  see if a similar change was already scoped.
- `opencode.jsonc` — opencode-specific agent permissions; requires approval
  to read `*.env`, `*oauth2.json`, the token file, `client_secrets.json`. A second, separate
  agent-config file from this one, relevant to any agent (not just Claude
  Code) working in this repo.

## Gotchas

- **`update` only touches allowlisted channels**, but even with an *empty*
  allowlist it still advances `last_updated` to (run start - 3 h) (it only skips
  fetch/insert, not the watermark write). `subscriptions add` must be run
  first (interactive fuzzy multi-select) — otherwise later-added channels
  silently miss everything published before the empty run. Allowlisted
  channels you've unsubscribed from are skipped with a warning (never
  auto-removed).
- **`--until` and `--auto-batch` are mutually exclusive** (raises
  `BadParameter`): `--until` sets a fixed cutoff, `--auto-batch` computes its
  own cutoff from the quota cap below.
- **`self.youtube` is thread-local**, not a plain cached property.
  `httplib2.Http` isn't thread-safe (shared connection cache), and channel
  fetches run concurrently via `asyncio.to_thread`; sharing one client across
  threads causes hangs or heap corruption. Don't refactor this into a normal
  `cached_property`.
- **OAuth is lazy** (`_credentials` is a `cached_property`, not eager in
  `__init__`) so that `subscriptions list` (without `--check`) and `remove` —
  local-file-only — never trigger a browser OAuth flow. `cached_property` has
  no lock, so whatever resolves it first must run on the main thread before
  any `asyncio.to_thread` fan-out — `update()` does this via
  `get_watchlater_playlist()` before fetching. A future call site that
  resolves credentials from inside a spawned thread could run the OAuth flow
  more than once.
- **Batch failures are all-or-nothing by design**, not a bug to "fix" with
  partial retry: `fetch_all_channels_videos` aborts the whole fetch on any
  channel error, and `insert_videos_watch_later` aborts the whole insert on
  any hard failure. This guarantees `last_updated` is never advanced past
  videos the run never actually saw/inserted.
- **Watermark lags 3 hours** (`PUBLISH_DELAY_HOURS`): `update` fetches only
  up to `now - 3 h` and records that as `last_updated`, so videos published
  mid-run or listed late by YouTube are picked up next run. A run within 3 h
  of the last one fetches nothing and leaves `last_updated` unchanged; the
  automatic (no `--since`/`--until`) path never moves it backwards, but an
  explicit `--since` plus an older `--until` (a backfill) still overwrites it;
  an older `--until` alone fetches nothing and leaves it unchanged.
  Known gap: a video made public long after upload may carry an older date
  and be missed; no documented field says when a video went public.
- **Quota-aware batching**: `MAX_INSERTS_PER_RUN = 160` (80% of the 10k daily
  quota / 50 cost per insert). `update --auto-batch` caps a run there by
  setting `last_updated` to the `published_at` of the first video *not*
  queued, not "now" — the cutoff is a timestamp, not a count, so ties at the
  boundary can queue fewer than 160. The remainder is picked up next run.
- **Inserts are serial, not concurrent** (unlike fetches): concurrent writes
  to the same playlist can trigger spurious 409s that look identical to a
  real duplicate, which the 409-skip handling can't tell apart. Insert order
  doesn't matter for correctness — `sort` sets final position separately.
- **`sort` only moves out-of-place items** (`plan_moves`: keep a longest
  increasing subsequence in place, move the rest). Each
  `playlistItems.update` costs 50 quota units; re-positioning every item
  exhausted the daily quota at ~200 videos. Moves are keyed by playlist item
  id, since the same video can appear twice.
  The sort key comes from the playlist items (`snippet.videoOwnerChannelId`,
  `contentDetails.videoPublishedAt`), not `videos.list`. Entries missing
  either (deleted/private videos) are moved to the front, first, and never
  deleted; removing them is manual. Setting positions requires the
  playlist's ordering to be Manual (`manualSortRequired` otherwise).
- `print` is rebound to `tqdm.write` at module level so plain `print()` calls
  don't corrupt progress bars — don't reassign or shadow this.
- `'Sort Watch Later'` is a **regular user-created playlist**, distinct from
  YouTube's built-in "Watch Later" — the code asserts this distinction, it
  does not attempt to touch the built-in one.
  `update` (dry run included) looks it up first and exits before fetching if missing.
- `read_config()` and `read_state()` are `@cache`, and `get_watchlater_playlist()` is `@lru_cache(1)`:
  config is read from disk once per process and the in-memory dict is
  mutated in place thereafter (`write_config` doesn't invalidate the cache,
  it doesn't need to — same dict object). `write_config` is atomic
  (`NamedTemporaryFile` next to the target + `os.replace`); an existing file
  keeps its permissions, a new one is created 0600 (`save_token` opts into
  forcing 0600 always, since it holds a refresh token); `read_config` never
  creates the file. The write target is resolved first, so a symlinked
  config/state/token file is written through the link.
  `get_watchlater_playlist` pages through all playlists (`playlists.list`
  defaults to 5 per page) and `sys.exit`s if none is titled `SORT_PLAYLIST_TITLE`.
- The Typer group callback (`main`) skips `migrate_legacy_config` when
  `--help` is in argv: Click runs the group callback before parsing a
  subcommand's own `--help`, so without this check `update --help` performed
  the real (non-dry-run) migration as a side effect.

## Code style

- Ruff: line length 120, `quote-style = "preserve"` (don't normalize quotes
  in diffs), lint rules `E, F, I, T10, UP, W`.
- mypy: `ignore_missing_imports = true` (many deps here are untyped).
- Python `>=3.14` per `pyproject.toml`. Use builtin generics and `X | None`
  (ruff `UP` rules enforce this); no `typing.Dict/List/Optional`.
