# 🎬 YouTube Sort Playlist

> A small CLI to help keep a **Sort Watch Later** playlist clean and grouped by channel.

## ✨ What it does

- Adds recent uploads from your subscribed channels to your `Sort Watch Later` playlist
- Sorts that playlist by channel and publish date
- Stores lightweight local state for auto-add preferences and last update time

## 🚀 Quick start

### 1) Prerequisites

- Python `~=3.9`
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
uv run playlist_updates.py update --dry-run -f
uv run playlist_updates.py sort --dry-run
```

Notes:

- `update` discovers subscriptions and can prompt to allow channels for auto-add
- `-f/--only-allowed` skips prompts and only uses previously allowed channels
- `--dry-run` prints actions without mutating playlists

## 🗂️ Config and state

The app stores config at:

- `$XDG_CACHE_HOME/youtube-sort-playlist/config.yaml`

It records allowed channels (`auto_add`) and `last_updated` timestamps.

## 🛠️ Development

Run autofixes:

```bash
make fix
```

Run checks:

```bash
make check
```

`pre-commit` is the primary autofix entrypoint; `make fix` wraps `pre-commit run --all-files`.

## 📦 Dependency refresh

```bash
uv lock --upgrade
make venv
make fix
make check
```

This project intentionally keeps its Python compatibility floor. Runtime dependencies should only be
upgraded as far as they can go without requiring application changes.
