# 🎬 YouTube Sort Playlist

> A CLI tool to automate organizing a YouTube playlist.

## ✨ What it does

- Adds recent uploads from your subscribed channels to your `Sort Watch Later` playlist
- Sorts that playlist by channel and publish date

## 🚀 Quick start

### 1) Prerequisites

- Python `>=3.14` (uv installs it if missing)
- [`uv`](https://docs.astral.sh/uv/)
- A YouTube Data API OAuth app with a `client_secrets.json` file in the project root

### 2) Set up your local environment

```bash
make venv
```

### 3) Add videos to `Sort Watch Later`

```bash
make update
```

### 4) Sort the playlist

```bash
make sort
```

## ⚙️ Usage details

Use the script directly for extra options:

```bash
uv run playlist_updates.py update --since 2026-01-01
uv run playlist_updates.py --dry-run update
uv run playlist_updates.py subscriptions add
uv run playlist_updates.py subscriptions list
uv run playlist_updates.py subscriptions list --check
uv run playlist_updates.py subscriptions remove
uv run playlist_updates.py --dry-run sort
```

Notes:

- `update` only pulls videos from channels already in the `subscriptions` allowlist
- `subscriptions add`/`remove` manage that allowlist interactively (fuzzy multi-select); `subscriptions list` shows it (`--check` also marks channels you've unsubscribed from; this signs in)
- `--dry-run` prints actions without mutating playlists or the allowlist

## 🗂️ Config and state

The app stores config at:

- Allowlist (`auto_add`): `$XDG_CONFIG_HOME/youtube-sort-playlist/config.yaml`
- Watermark (`last_updated`): `$XDG_STATE_HOME/youtube-sort-playlist/state.yaml`

A config left at the old `$XDG_CACHE_HOME/youtube-sort-playlist/config.yaml` is copied to the new locations on the
first real (non-dry) run; the old file is kept, and each run reminds you to delete it.

OAuth token: `$XDG_CONFIG_HOME/youtube-sort-playlist/token.json` (owner-only). The first run opens a
browser for consent. Upgrading from the oauth2client version asks for consent once; the old
`playlist_updates.py-oauth2.json` is no longer used and can be deleted.

## 🛠️ Development

Run autofixes:

```bash
make fix
```

Run checks (ruff, mypy, unit tests):

```bash
make check
```

## 📦 Dependency refresh

```bash
uv lock --upgrade
make venv
make fix
make check
```
