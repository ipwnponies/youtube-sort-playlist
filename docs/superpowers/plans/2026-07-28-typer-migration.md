# Typer Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `playlist_updates.py`'s argparse-based CLI (subparsers + broken `REMAINDER`-based passthrough) with Typer, and drop the now-confirmed-unused oauth2client unknown-args passthrough.

**Architecture:** A single `typer.Typer()` app replaces `parse_args()`/`main()`. `sort` and `update` become `@app.command()` functions. `--dry-run` becomes a shared `@app.callback()` option stored on `typer.Context`, read by both commands (global flag, precedes the subcommand). `--until`/`--auto-batch` mutual exclusivity is enforced with a manual check in the `update` command body. `get_creds()` and `YoutubeManager.__init__` drop their `args: List[str]` parameters since the passthrough they existed for is being removed.

**Tech Stack:** Python 3.11+, Typer (new dependency, pulls in Click), existing `arrow`/`oauth2client`/`googleapiclient` stack unchanged.

## Global Constraints

- No new dependency beyond `typer` (per spec — no `click-option-group` or similar).
- No automated test suite exists in this repo (`make test` is a no-op) — verification is manual CLI invocation, not pytest.
- `make check` (`ruff check .` + `mypy playlist_updates.py`) must pass after every task.
- Do not change `sort`/`update` business logic (`YoutubeManager.sort()`, `YoutubeManager.update()`) — only the CLI parsing layer and credential-loading signatures.
- `--dry-run` moves to a global option before the subcommand (idiomatic Typer, approved deviation from old per-subcommand placement).

---

### Task 1: Add Typer dependency and migrate CLI parsing

**Files:**
- Modify: `pyproject.toml` (via `uv add typer` — adds dependency + updates `uv.lock`)
- Modify: `playlist_updates.py:1-26` (imports)
- Modify: `playlist_updates.py:383-429` (replace `parse_args()` and `main()`)

**Interfaces:**
- Consumes: `YoutubeManager(dry_run: bool, args: List[str])` — existing constructor, unchanged in this task (Task 2 removes `args`). Existing `YoutubeManager.sort() -> None`.
- Produces: A runnable Typer `app` (module-level `typer.Typer()` instance) invoked via `if __name__ == '__main__': app()`. Command functions pass `[]` as the temporary `args` value to `YoutubeManager(...)` (Task 2 removes this parameter entirely). `YoutubeManager.update`'s `uploaded_after` parameter is retyped from `arrow.Arrow` to `Optional[arrow.Arrow]` in this task (see Step 3) — later tasks and any future caller should treat it as `Optional[arrow.Arrow], Optional[arrow.Arrow] = None, bool = False, bool = False`.

- [ ] **Step 1: Add the dependency**

Run: `uv add typer`

Expected: `pyproject.toml` gains a `typer` entry under `[project.dependencies]` (or equivalent), `uv.lock` is regenerated. No source changes yet.

- [ ] **Step 2: Update imports**

In `playlist_updates.py`, remove line 2 (`import argparse`), remove `Tuple` from the `typing` import on line 11 (no longer needed once `parse_args` returning a tuple is gone), and add `import typer` to the third-party import block (alphabetically, after `import oauth2client.tools` and before `import yaml`):

```python
#! /usr/bin/env python
import asyncio
import operator
import os
import sys
import threading
from collections import namedtuple
from functools import lru_cache, reduce
from pathlib import Path
from typing import Any, Dict, List, Optional

import addict
import arrow
import googleapiclient.errors
import httplib2
import oauth2client.client
import oauth2client.file
import oauth2client.tools
import typer
import yaml
from apiclient.discovery import build
from isodate import parse_duration, strftime
from tqdm import tqdm
from xdg import XDG_CACHE_HOME
```

- [ ] **Step 3: Replace `parse_args()` and `main()` with a Typer app**

Delete the entire `parse_args()` function (`playlist_updates.py:383-417`) and `main()` function (`playlist_updates.py:420-429`, i.e. everything from `def parse_args()` to the end of `main()`, but stop before the trailing `if __name__ == '__main__': main()` line — that line's replacement is in this same step). Replace with:

```python
app = typer.Typer(help='Tool to manage Youtube Watch Later playlist. Because they refuse to make it trivial.')


@app.callback()
def main(ctx: typer.Context, dry_run: bool = typer.Option(False, '--dry-run')) -> None:
    ctx.obj = dry_run


@app.command()
def sort(ctx: typer.Context) -> None:
    """Sort 'Watch Later' playlist."""
    youtube_manager = YoutubeManager(ctx.obj, [])
    youtube_manager.sort()


@app.command()
def update(
    ctx: typer.Context,
    since: Optional[str] = typer.Option(None, '--since', help='Start date to filter videos by.'),
    until: Optional[str] = typer.Option(None, '--until', help='End date to filter videos by.'),
    auto_batch: bool = typer.Option(False, '--auto-batch', help='Auto-chunk inserts to stay within API quota.'),
    only_allowed: bool = typer.Option(
        False, '-f', '--only-allowed', help='Auto add videos from known and allowed channels.'
    ),
) -> None:
    """Add recent videos to watch later playlist."""
    if until and auto_batch:
        raise typer.BadParameter('--until and --auto-batch are mutually exclusive.')

    youtube_manager = YoutubeManager(ctx.obj, [])
    youtube_manager.update(
        arrow.get(since) if since else None,
        arrow.get(until) if until else None,
        auto_batch,
        only_allowed,
    )


if __name__ == '__main__':
    app()
```

Note: `since`/`until` are typed `Optional[str]` with manual `arrow.get()` conversion in the command body rather than `type=arrow.get` (argparse's mechanism) — Typer/Click option types must be types Click knows how to convert from a CLI string (str, int, bool, Path, etc.), not arbitrary callables, so the conversion happens explicitly inside the function body instead.

Also update `YoutubeManager.update`'s signature at `playlist_updates.py:287-293`. Its `uploaded_after` parameter is currently typed as non-Optional `arrow.Arrow`, even though the method already handles `None` internally (`if uploaded_after is None: ...` a few lines into the body) — this was never caught by mypy because argparse's `Namespace` attributes are untyped (`Any`), so passing `None` through the old `args.since` silently satisfied any parameter type. Typer's `since` parameter is concretely typed `Optional[str]`, so `arrow.get(since) if since else None` is a real `Optional[Arrow]` that mypy will check against the parameter type. Fix the annotation to match actual behavior:

```python
    def update(
        self,
        uploaded_after: Optional[arrow.Arrow],
        uploaded_until: Optional[arrow.Arrow] = None,
        auto_batch: bool = False,
        only_allowed: bool = False,
    ) -> None:
```

(Only the type hint on `uploaded_after` changes, from `arrow.Arrow` to `Optional[arrow.Arrow]` — no behavior change, since the method body already treats it as possibly `None`.)

- [ ] **Step 4: Verify `make check` passes**

Run: `make check`
Expected: `ruff check .` reports "All checks passed!" and `mypy playlist_updates.py` reports "Success: no issues found in 1 source file".

- [ ] **Step 5: Manual verification**

Run: `uv run playlist_updates.py --help`
Expected: shows `sort` and `update` as available commands, plus `--dry-run` as a top-level option.

Run: `uv run playlist_updates.py update --help`
Expected: shows `--since`, `--until`, `--auto-batch`, `-f`/`--only-allowed` options.

Run: `uv run playlist_updates.py --dry-run update -f`
Expected: runs to completion without crashing (same behavior as before the migration — fetches channels, dry-run prints "Adding video to playlist: ..." lines without inserting).

Run: `uv run playlist_updates.py --dry-run update --since 2026-05-22 --auto-batch`
Expected: runs to completion; if backlog exceeds `MAX_INSERTS_PER_RUN`, prints a "Batch incomplete: ..." line same as before.

Run: `uv run playlist_updates.py --dry-run update --until 2026-01-01 --auto-batch`
Expected: fails fast with a `BadParameter`-style error mentioning `--until and --auto-batch are mutually exclusive.` — no channels are fetched.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock playlist_updates.py
git commit -m "feat: migrate CLI parsing from argparse to Typer

Replaces the argparse subparsers + REMAINDER-positional setup with a
Typer app. --dry-run becomes a shared global option (Typer idiom)
instead of per-subcommand. --until/--auto-batch mutual exclusivity is
now a manual check, since Typer/Click has no built-in equivalent to
argparse's mutually-exclusive groups."
```

---

### Task 2: Drop the oauth2client unknown-args passthrough

**Files:**
- Modify: `playlist_updates.py:72-92` (`YoutubeManager.__init__` and `get_creds`)
- Modify: `playlist_updates.py` (the two `YoutubeManager(ctx.obj, [])` call sites added in Task 1, in the `sort` and `update` commands)

**Interfaces:**
- Consumes: Task 1's `sort`/`update` command functions, which currently call `YoutubeManager(ctx.obj, [])`.
- Produces: `YoutubeManager(dry_run: bool)` — one-argument constructor. `get_creds() -> oauth2client.client.Credentials` — zero-argument static method.

- [ ] **Step 1: Simplify `get_creds`**

Locate `playlist_updates.py:78-92`:

```python
    @staticmethod
    def get_creds(args: List[str]) -> oauth2client.client.Credentials:
        """Authorize client with OAuth2."""
        flow = oauth2client.client.flow_from_clientsecrets(
            CLIENT_SECRETS_FILE, message=MISSING_CLIENT_SECRETS_MESSAGE, scope=YOUTUBE_READ_WRITE_SCOPE
        )

        storage = oauth2client.file.Storage(f'{sys.argv[0]}-oauth2.json')
        credentials = storage.get()

        if credentials is None or credentials.invalid:
            flags = oauth2client.tools.argparser.parse_args(args)
            credentials = oauth2client.tools.run_flow(flow, storage, flags)

        return credentials
```

Replace with:

```python
    @staticmethod
    def get_creds() -> oauth2client.client.Credentials:
        """Authorize client with OAuth2."""
        flow = oauth2client.client.flow_from_clientsecrets(
            CLIENT_SECRETS_FILE, message=MISSING_CLIENT_SECRETS_MESSAGE, scope=YOUTUBE_READ_WRITE_SCOPE
        )

        storage = oauth2client.file.Storage(f'{sys.argv[0]}-oauth2.json')
        credentials = storage.get()

        if credentials is None or credentials.invalid:
            flags = oauth2client.tools.argparser.parse_args([])
            credentials = oauth2client.tools.run_flow(flow, storage, flags)

        return credentials
```

- [ ] **Step 2: Simplify `__init__`**

Locate `playlist_updates.py:72-76`:

```python
class YoutubeManager:
    def __init__(self, dry_run: bool, args: List[str]) -> None:
        self.dry_run = dry_run
        self._credentials = self.get_creds(args)
        self._thread_local = threading.local()
```

Replace with:

```python
class YoutubeManager:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self._credentials = self.get_creds()
        self._thread_local = threading.local()
```

- [ ] **Step 3: Update call sites from Task 1**

In the `sort` and `update` command functions added in Task 1, change:

```python
youtube_manager = YoutubeManager(ctx.obj, [])
```

to:

```python
youtube_manager = YoutubeManager(ctx.obj)
```

in both places.

- [ ] **Step 4: Verify `make check` passes**

Run: `make check`
Expected: `ruff check .` reports "All checks passed!" and `mypy playlist_updates.py` reports "Success: no issues found in 1 source file".

- [ ] **Step 5: Manual verification**

Run: `uv run playlist_updates.py --dry-run update -f`
Expected: runs to completion identically to Task 1's Step 5 verification — confirms credential loading still works with the simplified signature.

Run: `uv run playlist_updates.py --dry-run sort`
Expected: runs to completion (fetches and dry-run "sorts" the watch-later playlist) without crashing.

- [ ] **Step 6: Commit**

```bash
git add playlist_updates.py
git commit -m "refactor: drop unused oauth2client CLI-args passthrough

The REMAINDER-based passthrough this existed for is gone (replaced by
Typer in the previous commit), and the passthrough itself was
confirmed unused in practice. get_creds() now sources oauth2client's
own flag defaults directly instead of threading an args list through
from main()."
```

## Out of scope (per spec)

- Splitting `playlist_updates.py` into multiple files.
- Adding automated tests.
- Any change to `YoutubeManager.sort()` / `YoutubeManager.update()` business logic.
