# Subscriptions Subcommand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split channel-allowlist management out of `update` into a new `subscriptions` Typer sub-app (`add`/`list`/`remove`), using `InquirerPy` fuzzy multi-select prompts and a `rich` table, and remove the now-dead `-f`/`--only-allowed` flag from `update`.

**Architecture:** A new `subscriptions_app = typer.Typer()` is mounted on the root `app` via `app.add_typer(subscriptions_app, name='subscriptions')`. It inherits the root `--dry-run` flag through Click's automatic `ctx.obj` inheritance (a child `Context`'s `obj` defaults to its parent's `obj` — the same mechanism `sort`/`update` already rely on one level down). Three new `YoutubeManager` methods (`add_subscriptions`, `list_subscriptions`, `remove_subscription`) hold the actual logic, called from three thin `@subscriptions_app.command()` functions. `update()` loses its interactive add-prompt block and `only_allowed` parameter entirely.

**Tech Stack:** Python 3.11+, existing Typer app, new dependencies `rich` (table rendering) and `InquirerPy` (fuzzy multi-select prompts).

## Global Constraints

- New dependencies limited to `rich` and `InquirerPy` — approved by user for this feature, per `docs/superpowers/specs/2026-07-29-subscriptions-subcommand-design.md`. No other new dependencies.
- No automated test suite exists in this repo (`make test` is a no-op) — verification is manual CLI invocation, not pytest.
- `make check` (`ruff check .` + `mypy playlist_updates.py`) must pass after every task.
- `auto_add` config shape is unchanged: a list of `{'id': str, 'name': str}` dicts.
- `--dry-run` semantics for `add`/`remove`: still fetch data and run the interactive prompt, but skip `write_config` — matches the existing convention where today's add-prompt block already gates its `write_config` call behind `not dry_run`.
- Do not add a `rename` command or any other subscriptions operation beyond `add`/`list`/`remove` — out of scope per spec.

---

### Task 1: Add dependencies and implement `subscriptions add`

**Files:**
- Modify: `pyproject.toml` (via `uv add rich InquirerPy`)
- Modify: `playlist_updates.py:1-26` (imports)
- Modify: `playlist_updates.py` (new `add_subscriptions` method on `YoutubeManager`, inserted directly after `get_subscribed_channels`, which currently ends at line 194 with `return channels`)
- Modify: `playlist_updates.py` (new `subscriptions` Typer sub-app + `add` command, added near the end of the file, before `if __name__ == '__main__':`)

**Interfaces:**
- Consumes: `YoutubeManager.get_subscribed_channels() -> List[Dict[str, str]]` (existing, returns `[{'title': ..., 'id': ...}, ...]`), `read_config() -> JsonType`, `write_config(config: JsonType) -> None` (existing module functions).
- Produces: `YoutubeManager.add_subscriptions() -> None`. `subscriptions_app` (module-level `typer.Typer()` instance, mounted on `app` as `subscriptions`) — Tasks 2 and 3 add their commands to this same instance.

- [ ] **Step 1: Add the dependencies**

Run: `uv add rich InquirerPy`

Expected: `pyproject.toml` gains `rich` and `InquirerPy` entries under `[project.dependencies]`, `uv.lock` is regenerated. No source changes yet.

- [ ] **Step 2: Verify the InquirerPy API surface this plan depends on**

Run: `uv run python -c "from InquirerPy import inquirer; from InquirerPy.base.control import Choice; from rich.table import Table; from rich.console import Console; print('ok')"`

Expected: prints `ok` with no `ImportError`. If `Choice` is not importable from `InquirerPy.base.control` in the installed version, stop and check `uv run python -c "import InquirerPy; print(InquirerPy.__file__)"` to locate the installed package and find the correct import path before continuing — do not guess further.

- [ ] **Step 3: Add imports**

In `playlist_updates.py`, add the following imports to the third-party import block (alphabetically):

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
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from isodate import parse_duration, strftime
from rich.console import Console
from rich.table import Table
from tqdm import tqdm
from xdg import XDG_CACHE_HOME
```

- [ ] **Step 4: Add `add_subscriptions` to `YoutubeManager`**

Locate the end of `get_subscribed_channels` (currently `playlist_updates.py:183-194`):

```python
    def get_subscribed_channels(self) -> List[Dict[str, str]]:
        channels: List[Dict[str, str]] = []
        next_page_token = None
        request = self.youtube.subscriptions().list(part='snippet', mine=True, maxResults=50, pageToken=next_page_token)

        while request:
            response = request.execute()
            response = addict.Dict(response)
            channels.extend({'title': i.snippet.title, 'id': i.snippet.resourceId.channelId} for i in response['items'])
            request = self.youtube.subscriptions().list_next(request, response)

        return channels
```

Insert a new method directly after it (before `get_channel_details`):

```python

    def add_subscriptions(self) -> None:
        """Interactively add newly-subscribed channels to the auto-add list."""
        channels = self.get_subscribed_channels()
        config = read_config()
        auto_add = config.setdefault('auto_add', [])
        known_ids = {i['id'] for i in auto_add}

        candidates = [i for i in channels if i['id'] not in known_ids]
        if not candidates:
            print('No new channels to add.')
            return

        choices = [Choice(channel, name=channel['title']) for channel in candidates]
        selected = inquirer.fuzzy(
            message='Select channels to add:',
            choices=choices,
            multiselect=True,
        ).execute()

        if not selected:
            print('Nothing selected.')
            return

        auto_add.extend({'id': channel['id'], 'name': channel['title']} for channel in selected)

        if not self.dry_run:
            write_config(config)

        print(f"Added {len(selected)} channel(s): {', '.join(channel['title'] for channel in selected)}")
```

- [ ] **Step 5: Wire up the `subscriptions` sub-app and `add` command**

Near the end of `playlist_updates.py`, directly after the existing `update` command function and before `if __name__ == '__main__':`, add:

```python
subscriptions_app = typer.Typer(help='Manage channels allowed to auto-add videos.')
app.add_typer(subscriptions_app, name='subscriptions')


@subscriptions_app.command('add')
def subscriptions_add(ctx: typer.Context) -> None:
    """Interactively add newly-subscribed channels."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.add_subscriptions()
```

- [ ] **Step 6: Verify `make check` passes**

Run: `make check`
Expected: `ruff check .` reports "All checks passed!" and `mypy playlist_updates.py` reports "Success: no issues found in 1 source file".

- [ ] **Step 7: Manual verification**

Run: `uv run playlist_updates.py subscriptions --help`
Expected: shows `add` as an available subcommand (plus `list`/`remove` once Tasks 2-3 land).

Run: `uv run playlist_updates.py subscriptions add --help`
Expected: shows the command description, no errors.

Run: `uv run playlist_updates.py --dry-run subscriptions add`
Expected: fetches subscribed channels; if any aren't already in `config.yaml`'s `auto_add`, a fuzzy-search multiselect prompt appears (type to filter, space to select, enter to confirm). After confirming a selection, prints an "Added N channel(s): ..." summary. Then check `$XDG_CACHE_HOME/youtube-sort-playlist/config.yaml` (or `~/.cache/youtube-sort-playlist/config.yaml` if `XDG_CACHE_HOME` is unset) and confirm it was **not** modified (dry-run).

Run: `uv run playlist_updates.py subscriptions add` (no `--dry-run`), select at least one channel, confirm.
Expected: `config.yaml`'s `auto_add` list now contains the selected channel(s) as `{'id': ..., 'name': ...}` entries.

Run: `uv run playlist_updates.py subscriptions add` again immediately after.
Expected: if all subscribed channels are now known, prints "No new channels to add." and exits without prompting.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock playlist_updates.py
git commit -m "feat: add 'subscriptions add' subcommand

Introduces InquirerPy for a fuzzy multi-select prompt so new channel
approval doesn't require the old per-channel y/n loop. This is the
first piece of pulling channel-allowlist management out of update."
```

---

### Task 2: Implement `subscriptions list`

**Files:**
- Modify: `playlist_updates.py` (new `list_subscriptions` method on `YoutubeManager`, inserted directly after `add_subscriptions` from Task 1)
- Modify: `playlist_updates.py` (new `list` command on `subscriptions_app`, added directly after the `add` command from Task 1)

**Interfaces:**
- Consumes: `read_config() -> JsonType` (existing). `subscriptions_app` (Task 1's Typer sub-app instance).
- Produces: `YoutubeManager.list_subscriptions() -> None`.

- [ ] **Step 1: Add `list_subscriptions` to `YoutubeManager`**

Directly after the `add_subscriptions` method added in Task 1, insert:

```python

    def list_subscriptions(self) -> None:
        """Print the channels currently allowed to auto-add videos."""
        config = read_config()
        auto_add = config.get('auto_add', [])

        if not auto_add:
            print('No subscriptions.')
            return

        table = Table('Name', 'Channel ID')
        for channel in auto_add:
            table.add_row(channel['name'], channel['id'])

        Console().print(table)
```

- [ ] **Step 2: Wire up the `list` command**

Directly after the `subscriptions_add` command function added in Task 1, add:

```python


@subscriptions_app.command('list')
def subscriptions_list(ctx: typer.Context) -> None:
    """List channels currently allowed to auto-add videos."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.list_subscriptions()
```

- [ ] **Step 3: Verify `make check` passes**

Run: `make check`
Expected: `ruff check .` reports "All checks passed!" and `mypy playlist_updates.py` reports "Success: no issues found in 1 source file".

- [ ] **Step 4: Manual verification**

Run: `uv run playlist_updates.py subscriptions list --help`
Expected: shows the command description, no errors.

Run: `uv run playlist_updates.py subscriptions list`
Expected: prints a table with `Name`/`Channel ID` columns, one row per entry in `config.yaml`'s `auto_add` (populated from Task 1's verification step).

Temporarily rename `auto_add` to something else in `config.yaml` (or otherwise empty it) and re-run `uv run playlist_updates.py subscriptions list`.
Expected: prints "No subscriptions." Then restore `config.yaml` to its prior state.

- [ ] **Step 5: Commit**

```bash
git add playlist_updates.py
git commit -m "feat: add 'subscriptions list' subcommand

Prints the current auto-add allowlist as a rich table, so channels
can be reviewed without opening config.yaml directly."
```

---

### Task 3: Implement `subscriptions remove`

**Files:**
- Modify: `playlist_updates.py` (new `remove_subscription` method on `YoutubeManager`, inserted directly after `list_subscriptions` from Task 2)
- Modify: `playlist_updates.py` (new `remove` command on `subscriptions_app`, added directly after the `list` command from Task 2)

**Interfaces:**
- Consumes: `read_config() -> JsonType`, `write_config(config: JsonType) -> None` (existing). `Choice`, `inquirer.fuzzy` (Task 1's imports). `subscriptions_app` (Task 1's Typer sub-app instance).
- Produces: `YoutubeManager.remove_subscription() -> None`.

- [ ] **Step 1: Add `remove_subscription` to `YoutubeManager`**

Directly after the `list_subscriptions` method added in Task 2, insert:

```python

    def remove_subscription(self) -> None:
        """Interactively remove channels from the auto-add list."""
        config = read_config()
        auto_add = config.setdefault('auto_add', [])

        if not auto_add:
            print('No subscriptions to remove.')
            return

        choices = [Choice(channel, name=channel['name']) for channel in auto_add]
        selected = inquirer.fuzzy(
            message='Select channels to remove:',
            choices=choices,
            multiselect=True,
        ).execute()

        if not selected:
            print('Nothing selected.')
            return

        removed_ids = {channel['id'] for channel in selected}
        config['auto_add'] = [channel for channel in auto_add if channel['id'] not in removed_ids]

        if not self.dry_run:
            write_config(config)

        print(f"Removed {len(selected)} channel(s): {', '.join(channel['name'] for channel in selected)}")
```

- [ ] **Step 2: Wire up the `remove` command**

Directly after the `subscriptions_list` command function added in Task 2, add:

```python


@subscriptions_app.command('remove')
def subscriptions_remove(ctx: typer.Context) -> None:
    """Interactively remove channels from the auto-add list."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.remove_subscription()
```

- [ ] **Step 3: Verify `make check` passes**

Run: `make check`
Expected: `ruff check .` reports "All checks passed!" and `mypy playlist_updates.py` reports "Success: no issues found in 1 source file".

- [ ] **Step 4: Manual verification**

Run: `uv run playlist_updates.py subscriptions remove --help`
Expected: shows the command description, no errors.

Run: `uv run playlist_updates.py --dry-run subscriptions remove`, select a channel, confirm.
Expected: prints a "Removed N channel(s): ..." summary. Check `config.yaml` and confirm `auto_add` was **not** changed (dry-run).

Run: `uv run playlist_updates.py subscriptions remove` (no `--dry-run`), select the same channel, confirm.
Expected: `config.yaml`'s `auto_add` no longer contains that channel. Confirm with `uv run playlist_updates.py subscriptions list`.

Empty `auto_add` entirely (remove all remaining entries, or edit `config.yaml` directly) and run `uv run playlist_updates.py subscriptions remove` again.
Expected: prints "No subscriptions to remove." and exits without prompting.

- [ ] **Step 5: Commit**

```bash
git add playlist_updates.py
git commit -m "feat: add 'subscriptions remove' subcommand

Completes the subscriptions sub-app: add/list/remove now cover
channel-allowlist management without needing to hand-edit config.yaml
or run update interactively."
```

---

### Task 4: Remove `update`'s interactive add-prompt and `-f`/`--only-allowed` flag

**Files:**
- Modify: `playlist_updates.py` (the `update` method on `YoutubeManager`, currently `playlist_updates.py:287-338`)
- Modify: `playlist_updates.py` (the `update` Typer command, currently `playlist_updates.py:398-424`)
- Modify: `Makefile` (the `update` target, currently invokes `update -f --auto-batch`)
- Modify: `README.md` (usage notes referencing `-f`/`--only-allowed` and the old prompt-based flow)

**Interfaces:**
- Consumes: nothing new — this task only removes code.
- Produces: `YoutubeManager.update(uploaded_after: Optional[arrow.Arrow], uploaded_until: Optional[arrow.Arrow] = None, auto_batch: bool = False) -> None` (drops the `only_allowed` parameter).

- [ ] **Step 1: Remove the prompt block from `YoutubeManager.update`**

Locate:

```python
    def update(
        self,
        uploaded_after: Optional[arrow.Arrow],
        uploaded_until: Optional[arrow.Arrow] = None,
        auto_batch: bool = False,
        only_allowed: bool = False,
    ) -> None:
        channels = self.get_subscribed_channels()
        config = read_config()
        auto_add = config.setdefault('auto_add', [])

        if uploaded_after is None:
            if 'last_updated' in config:
                uploaded_after = arrow.get(config['last_updated'])
            else:
                uploaded_after = arrow.now().shift(weeks=-2)

        allowed_channel_ids = {i['id'] for i in auto_add}

        if not only_allowed and not self.dry_run:
            unknown_channels = [i for i in channels if i['id'] not in allowed_channel_ids]
            for channel in unknown_channels:
                response = input(f'Want to auto-add videos from "{channel["title"]}"? y/n: ')
                if response == 'y':
                    auto_add.append({'id': channel['id'], 'name': channel['title']})
                    allowed_channel_ids.add(channel['id'])
            write_config(config)

        allowed_channels = [i for i in channels if i['id'] in allowed_channel_ids]
```

Replace with:

```python
    def update(
        self,
        uploaded_after: Optional[arrow.Arrow],
        uploaded_until: Optional[arrow.Arrow] = None,
        auto_batch: bool = False,
    ) -> None:
        channels = self.get_subscribed_channels()
        config = read_config()
        auto_add = config.setdefault('auto_add', [])

        if uploaded_after is None:
            if 'last_updated' in config:
                uploaded_after = arrow.get(config['last_updated'])
            else:
                uploaded_after = arrow.now().shift(weeks=-2)

        allowed_channel_ids = {i['id'] for i in auto_add}
        allowed_channels = [i for i in channels if i['id'] in allowed_channel_ids]
```

(The rest of `update` — building `all_videos`, the `auto_batch` chunking, `insert_videos_watch_later`, and updating `last_updated` — is unchanged.)

- [ ] **Step 2: Remove `-f`/`--only-allowed` from the `update` CLI command**

Locate:

```python
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

    try:
        since_arrow = arrow.get(since) if since else None
        until_arrow = arrow.get(until) if until else None
    except arrow.parser.ParserError as error:
        raise typer.BadParameter(str(error)) from error

    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.update(
        since_arrow,
        until_arrow,
        auto_batch,
        only_allowed,
    )
```

Replace with:

```python
@app.command()
def update(
    ctx: typer.Context,
    since: Optional[str] = typer.Option(None, '--since', help='Start date to filter videos by.'),
    until: Optional[str] = typer.Option(None, '--until', help='End date to filter videos by.'),
    auto_batch: bool = typer.Option(False, '--auto-batch', help='Auto-chunk inserts to stay within API quota.'),
) -> None:
    """Add recent videos to watch later playlist."""
    if until and auto_batch:
        raise typer.BadParameter('--until and --auto-batch are mutually exclusive.')

    try:
        since_arrow = arrow.get(since) if since else None
        until_arrow = arrow.get(until) if until else None
    except arrow.parser.ParserError as error:
        raise typer.BadParameter(str(error)) from error

    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.update(
        since_arrow,
        until_arrow,
        auto_batch,
    )
```

- [ ] **Step 3: Fix the Makefile's `update` target**

In `Makefile`, locate:

```makefile
.PHONY: update
update: venv  ## Add new videos to Watch Later
	uv run playlist_updates.py update -f --auto-batch
```

Replace with:

```makefile
.PHONY: update
update: venv  ## Add new videos to Watch Later
	uv run playlist_updates.py update --auto-batch
```

- [ ] **Step 4: Update README usage notes**

In `README.md`, locate:

```markdown
```bash
uv run playlist_updates.py update --since 2026-01-01
uv run playlist_updates.py update --dry-run -f
uv run playlist_updates.py sort --dry-run
```

Notes:

- `update` discovers subscriptions and can prompt to allow channels for auto-add
- `-f/--only-allowed` skips prompts and only uses previously allowed channels
- `--dry-run` prints actions without mutating playlists
```

Replace with:

```markdown
```bash
uv run playlist_updates.py update --since 2026-01-01
uv run playlist_updates.py update --dry-run
uv run playlist_updates.py subscriptions add
uv run playlist_updates.py subscriptions list
uv run playlist_updates.py subscriptions remove
uv run playlist_updates.py sort --dry-run
```

Notes:

- `update` only pulls videos from channels already in the `subscriptions` allowlist
- `subscriptions add`/`remove` manage that allowlist interactively (fuzzy multi-select); `subscriptions list` shows it
- `--dry-run` prints actions without mutating playlists or the allowlist
```

- [ ] **Step 5: Verify `make check` passes**

Run: `make check`
Expected: `ruff check .` reports "All checks passed!" and `mypy playlist_updates.py` reports "Success: no issues found in 1 source file".

- [ ] **Step 6: Manual verification**

Run: `uv run playlist_updates.py update --help`
Expected: shows only `--since`, `--until`, `--auto-batch` — no `-f`/`--only-allowed`.

Run: `uv run playlist_updates.py --dry-run update`
Expected: runs to completion, pulling only from channels currently in `config.yaml`'s `auto_add` (no prompting, regardless of any newly-subscribed-but-unknown channels).

Run: `uv run playlist_updates.py --dry-run update --since 2026-05-22 --auto-batch`
Expected: runs to completion; if backlog exceeds `MAX_INSERTS_PER_RUN`, prints a "Batch incomplete: ..." line, same as before this task.

Run: `uv run playlist_updates.py --dry-run update --until 2026-01-01 --auto-batch`
Expected: fails fast with a `BadParameter`-style error mentioning `--until and --auto-batch are mutually exclusive.` — unchanged from before this task.

Run: `make update` (or inspect with `make -n update`)
Expected: invokes `uv run playlist_updates.py update --auto-batch` without `-f`.

- [ ] **Step 7: Commit**

```bash
git add playlist_updates.py Makefile README.md
git commit -m "refactor: remove update's interactive add-prompt and -f flag

Channel-allowlist management now lives entirely in the subscriptions
subcommand (previous three commits). update always restricts to
channels already in auto_add, so -f/--only-allowed became a
permanent no-op and is removed along with the prompt loop it gated."
```

## Out of scope (per spec)

- Any config format change beyond what `add`/`remove` already touch.
- A `rename` command or any subscriptions operation beyond `add`/`list`/`remove`.
- Automated tests (none exist today; not part of this feature).
