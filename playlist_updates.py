#! /usr/bin/env python
import asyncio
import bisect
import os
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from datetime import timedelta
from functools import cached_property, lru_cache
from itertools import batched
from pathlib import Path
from typing import IO, Any

import addict
import arrow
import google.auth.exceptions
import google.auth.transport.requests
import googleapiclient.errors
import typer
import yaml
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from isodate import parse_duration
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from tqdm import tqdm
from xdg import xdg_cache_home, xdg_config_home

print = tqdm.write


# Resolved next to this script, not the cwd, so the tool works wherever it's invoked from (cron, aliases).
APP_DIR = Path(__file__).resolve().parent

# OAuth 2.0 client (client_id/client_secret) for a Google Cloud project with the YouTube Data API enabled. See:
#   https://developers.google.com/youtube/v3/guides/authentication
#   https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
CLIENT_SECRETS_FILE = APP_DIR / 'client_secrets.json'

# Token written by oauth2client (named after argv[0], normally 'playlist_updates.py'). No longer read; the user is
# told to delete it.
LEGACY_TOKEN_FILE = APP_DIR / 'playlist_updates.py-oauth2.json'

MISSING_CLIENT_SECRETS_MESSAGE = """
WARNING: Please configure OAuth 2.0

Create an OAuth client for a project with the YouTube Data API enabled in the Google Cloud Console
(https://console.cloud.google.com/apis/credentials) and save it as:

   {path}

For more information about the client_secrets.json file format, please visit:
https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
"""

APP_NAME = 'youtube-sort-playlist'
CONFIG_FILE = xdg_cache_home() / APP_NAME / 'config.yaml'

# Holds a refresh token with full YouTube access: kept outside the repo checkout, owner-only.
TOKEN_FILE = xdg_config_home() / APP_NAME / 'token.json'

YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube']
# oauth2client's default, so redirect URIs already registered on the OAuth client keep working.
OAUTH_REDIRECT_PORT = 8080
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'

SORT_PLAYLIST_TITLE = 'Sort Watch Later'

DAILY_QUOTA = 10_000
INSERT_COST = 50
MAX_INSERTS_PER_RUN = int(DAILY_QUOTA * 0.8 / INSERT_COST)  # 160
# Newest uploads are left for the next run: covers videos published mid-run and YouTube listing uploads late.
PUBLISH_DELAY_HOURS = 3

JsonType = dict[str, Any]


def longest_increasing_subsequence(values: list[int]) -> set[int]:
    """Return the indices of one longest strictly increasing subsequence of `values`.

    Patience sorting (binary search over pile tops) gives the length in O(n log n); predecessor links then
    reconstruct one actual subsequence, not just its length.
    """
    tails: list[int] = []  # tails[k]: smallest tail value of any increasing subsequence of length k + 1
    tail_indices: list[int] = []
    predecessors: dict[int, int | None] = {}  # parent pointers: index -> index of the previous element in its run
    for index, value in enumerate(values):
        length = bisect.bisect_left(tails, value)
        predecessors[index] = tail_indices[length - 1] if length else None
        if length == len(tails):
            tails.append(value)
            tail_indices.append(index)
        else:
            # Only each pile's top is kept, not the whole pile: LIS needs just the smallest tail per length.
            tails[length] = value
            tail_indices[length] = index

    # Walk predecessor links back from the tail of the longest pile to collect the LIS indices.
    result: set[int] = set()
    cursor = tail_indices[-1] if tail_indices else None
    while cursor is not None:
        result.add(cursor)
        cursor = predecessors[cursor]
    return result


def plan_moves(current: list[str], target: list[str]) -> list[tuple[str, int]]:
    """Plan the fewest `(item_id, position)` moves that reorder `current` into `target`.

    A move mirrors a playlistItems.update with a new position: the item is taken out and reinserted at `position`,
    shifting everything in between. Every update costs 50 quota units, so items already in the right order relative
    to each other (a longest increasing subsequence of their target ranks) stay put and only the rest move. Each
    moved item is placed directly after its target predecessor, which leaves the whole list in target order.
    """
    # Rank transform: replace each id with its index in `target`, so ordering is plain integer comparison.
    rank = {item_id: index for index, item_id in enumerate(target)}
    current_ranks = [rank[item_id] for item_id in current]
    # Items on a longest increasing run of ranks are already in correct relative order: they never move.
    stable = {current[i] for i in longest_increasing_subsequence(current_ranks)}

    # `order` replays each move locally, because positions are absolute and shift after every update;
    # `moves` is the log sent to the API.
    order = list(current)
    moves: list[tuple[str, int]] = []
    # Visiting target in order means target[index - 1] is already placed, so inserting right after it is final.
    for index, item_id in enumerate(target):
        if item_id in stable:
            continue
        order.remove(item_id)
        position = order.index(target[index - 1]) + 1 if index else 0
        order.insert(position, item_id)
        moves.append((item_id, position))
    return moves


def is_sortable(playlist_item: JsonType) -> bool:
    """Whether the entry carries its uploader channel and publish date (deleted/private videos may not)."""
    return bool(
        playlist_item['snippet'].get('videoOwnerChannelId')
        and playlist_item.get('contentDetails', {}).get('videoPublishedAt')
    )


def humanize_duration(duration: timedelta) -> str:
    """Human-readable duration, e.g. 'a day 6 hours and 5 minutes', using arrow's humanize.

    arrow humanizes the distance between two instants, so the duration is laid out from a fixed base instant.
    """
    base = arrow.Arrow(2000, 1, 1)
    return (base + duration).humanize(base, only_distance=True, granularity=['day', 'hour', 'minute'])


def stale_channels(auto_add: list[dict[str, str]], subscribed: list[dict[str, str]]) -> list[dict[str, str]]:
    """Allowlisted channels that are not among the current subscriptions."""
    subscribed_ids = {i['id'] for i in subscribed}
    return [i for i in auto_add if i['id'] not in subscribed_ids]


class YoutubeManager:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self._thread_local = threading.local()

    @staticmethod
    def get_creds() -> Credentials:
        """Load cached OAuth credentials, refreshing them or re-running browser consent as needed."""
        credentials = None
        if TOKEN_FILE.exists():
            try:
                credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), YOUTUBE_SCOPES)
            except ValueError, AttributeError:
                credentials = None  # Corrupt or hand-edited token file: ask for consent again.
        if credentials is not None:
            if credentials.valid:
                return credentials

            if credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(google.auth.transport.requests.Request())
                except google.auth.exceptions.RefreshError:
                    credentials = None  # Revoked or expired refresh token: ask for consent again.
            else:
                credentials = None

        if credentials is None:
            if not CLIENT_SECRETS_FILE.exists():
                sys.exit(MISSING_CLIENT_SECRETS_MESSAGE.format(path=CLIENT_SECRETS_FILE))
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), YOUTUBE_SCOPES)
            credentials = flow.run_local_server(port=OAUTH_REDIRECT_PORT)

        save_token(credentials)
        return credentials

    @cached_property
    def _credentials(self) -> Credentials:
        """Lazily-resolved OAuth credentials.

        Only commands that actually hit the YouTube API need these; local-file-only commands (e.g.
        `subscriptions list`/`remove`) must not trigger an OAuth flow just to construct a manager.
        """
        return self.get_creds()

    @property
    def youtube(self):
        """Thread-local youtube data v3 object.

        build() creates its own authorized httplib2 client per call, which is not thread-safe, so each
        thread builds and keeps its own service object.
        """
        if not hasattr(self._thread_local, 'youtube'):
            self._thread_local.youtube = build(
                YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=self._credentials
            )
        return self._thread_local.youtube

    @lru_cache(1)
    def get_watchlater_playlist(self) -> str:
        """Get the id of the 'Sort Watch Later' playlist.

        The 'Sort Watch Later' playlist is regular playlist and is not the same as the magical one that all
        youtube users have by default. Exits if the playlist doesn't exist.
        """
        # playlists.list returns 5 results per page by default, so page through all of them.
        request = self.youtube.playlists().list(part='snippet', mine=True, maxResults=50)
        while request:
            response = request.execute()
            for playlist in response['items']:
                if playlist['snippet']['title'] == SORT_PLAYLIST_TITLE:
                    return playlist['id']
            request = self.youtube.playlists().list_next(request, response)

        sys.exit(f"Oh noes, you don't have a playlist named {SORT_PLAYLIST_TITLE}")

    def get_playlist_videos(self, watchlater_id: str) -> list[JsonType]:
        """Returns list of playlistItems from Sort Watch Later playlist"""
        result: list[dict] = []

        request = self.youtube.playlistItems().list(
            part='snippet,contentDetails', playlistId=watchlater_id, maxResults=50
        )

        # Iterate through all results pages
        while request:
            response: dict[str, dict] = request.execute()

            result.extend(response['items'])

            # Prepare next results page
            request = self.youtube.playlistItems().list_next(request, response)
        return result

    def get_video_durations(self, playlist_videos: list[JsonType]) -> dict[str, timedelta]:
        """Returns each video's duration, keyed by video id. Videos videos.list returns nothing for are absent."""
        result: dict[str, timedelta] = {}
        videos = [i['snippet']['resourceId']['videoId'] for i in playlist_videos]

        # Partition videos due to max number of videos queryable with one api call
        for to_query in batched(videos, 50):
            response = self.youtube.videos().list(part='contentDetails', id=','.join(to_query), maxResults=50).execute()
            for i in response['items']:
                result[i['id']] = parse_duration(i['contentDetails']['duration'])

        return result

    def sort_playlist(self, playlist_videos: list[JsonType]) -> None:
        """Sorts a playlist and groups videos by channel.

        The sort key comes from the playlist items themselves (uploader channel, then publish date). Entries missing
        either field, such as deleted or private videos, go to the front in their current order: visible for manual
        cleanup, and moved before any live video, so an API refusal to move one stops the sort before anything else
        is reordered. Only out-of-place items are updated, since each playlistItems.update costs 50 quota units.
        """

        def sort_key(playlist_item: JsonType) -> tuple[bool, str, str]:
            """Unavailable entries first, then videos grouped by channel, sorted by date in ascending order."""
            if not is_sortable(playlist_item):
                return (False, '', '')
            return (
                True,
                playlist_item['snippet']['videoOwnerChannelId'],
                playlist_item['contentDetails']['videoPublishedAt'],
            )

        unavailable = sum(1 for i in playlist_videos if not is_sortable(i))
        if unavailable:
            print(f'{unavailable} unavailable (deleted/private) video(s) will be kept at the front of the playlist.')

        items_by_id = {i['id']: i for i in playlist_videos}
        target = [i['id'] for i in sorted(playlist_videos, key=sort_key)]
        moves = plan_moves([i['id'] for i in playlist_videos], target)
        print(f'{len(moves)} of {len(playlist_videos)} videos need to move.')

        for item_id, position in tqdm(moves, unit='video'):
            item = items_by_id[item_id]
            print(f"{item['snippet']['title']} is being put in pos {position}")

            if not self.dry_run:
                item['snippet']['position'] = position
                self.youtube.playlistItems().update(part='snippet', body=item).execute()

    def get_subscribed_channels(self) -> list[dict[str, str]]:
        channels: list[dict[str, str]] = []
        request = self.youtube.subscriptions().list(part='snippet', mine=True, maxResults=50)

        while request:
            response = request.execute()
            response = addict.Dict(response)
            channels.extend({'title': i.snippet.title, 'id': i.snippet.resourceId.channelId} for i in response['items'])
            request = self.youtube.subscriptions().list_next(request, response)

        return channels

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

    def list_subscriptions(self, check: bool = False) -> None:
        """Print the channels currently allowed to auto-add videos.

        With `check`, also fetch current subscriptions (signs in) and mark allowlisted channels no longer subscribed.
        Without it, this stays local-file-only.
        """
        config = read_config()
        auto_add = config.get('auto_add', [])

        if not auto_add:
            print('No subscriptions.')
            return

        stale_ids = {i['id'] for i in stale_channels(auto_add, self.get_subscribed_channels())} if check else set()
        table = Table('Name', 'Channel ID', *(['Subscribed'] if check else []))
        for channel in auto_add:
            row = [escape(channel['name']), escape(channel['id'])]
            if check:
                row.append('no' if channel['id'] in stale_ids else 'yes')
            table.add_row(*row)

        Console().print(table)

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

    def get_channel_details(self, channel_id: str) -> addict.Dict:
        request = self.youtube.channels().list(part='contentDetails', id=channel_id)

        # Only 1 item, since queried by id
        channel_details = addict.Dict(request.execute()['items'][0])
        return channel_details

    def fetch_channel_videos(
        self, channel: str, uploaded_after: arrow.Arrow, uploaded_until: arrow.Arrow | None = None
    ) -> list[JsonType]:
        videos = []

        channel_details = self.get_channel_details(channel)
        uploaded_playlist = channel_details.contentDetails.relatedPlaylists.uploads

        request = self.youtube.playlistItems().list(part='snippet', playlistId=uploaded_playlist, maxResults=50)

        while request:
            response = addict.Dict(request.execute())
            videos_on_page = [i for i in response['items'] if i.snippet.resourceId.kind == 'youtube#video']
            recent_videos = [
                {'id': i.snippet.resourceId.videoId, 'title': i.snippet.title, 'published_at': i.snippet.publishedAt}
                for i in videos_on_page
                if arrow.get(i.snippet.publishedAt) >= uploaded_after
                and (uploaded_until is None or arrow.get(i.snippet.publishedAt) < uploaded_until)
            ]

            videos.extend(recent_videos)

            # YouTube returns newest-first; stop when we've seen a video older than our window
            if any(arrow.get(i.snippet.publishedAt) < uploaded_after for i in videos_on_page):
                break

            request = self.youtube.playlistItems().list_next(request, response)

        return videos

    async def fetch_all_channels_videos(
        self, channels: list[dict[str, str]], uploaded_after: arrow.Arrow, uploaded_until: arrow.Arrow | None
    ) -> list[JsonType]:
        """Fetch each channel's recent videos concurrently.

        Fetching is a pure read with no ordering requirement, so channels are processed in parallel. A failure on
        any channel aborts the whole batch: a partial channel set must never reach the insert phase, since that
        would let `last_updated` advance past videos we never actually looked at.
        """
        tasks = [
            asyncio.to_thread(self.fetch_channel_videos, channel['id'], uploaded_after, uploaded_until)
            for channel in channels
        ]

        all_videos: list[JsonType] = []
        for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), unit='channel'):
            channel_videos = await task
            channel_videos.sort(key=lambda v: v['published_at'])
            all_videos.extend(channel_videos)

        return all_videos

    def add_video_to_watch_later(self, video: JsonType) -> None:
        print(f"Adding video to playlist: {video['title']}")
        if not self.dry_run:
            try:
                self.youtube.playlistItems().insert(
                    part='snippet',
                    body={
                        'snippet': {
                            'playlistId': self.get_watchlater_playlist(),
                            'resourceId': {'kind': 'youtube#video', 'videoId': video['id']},
                        }
                    },
                ).execute()
            except googleapiclient.errors.HttpError as error:
                if error.resp.status == 409:
                    print('Already in list, skipping!')
                else:
                    raise

    def insert_videos_watch_later(self, videos: list[JsonType]) -> None:
        """Insert videos one at a time.

        Concurrent writes to the same playlist can trip YouTube API conflict responses unrelated to the
        video actually being a duplicate, which the 409-skip handling below can't distinguish from a real
        duplicate; inserting serially avoids that race. Insert order doesn't affect correctness either way:
        playlist position is set later by `sort`, not by insert order. A hard failure on any video aborts
        the whole batch so that `update()` never mints `last_updated` for a partially-inserted batch; the
        next run retries the full batch, tolerating re-inserts via the existing 409-skip handling above.
        """
        for video in tqdm(videos, unit='video'):
            self.add_video_to_watch_later(video)

    def update(
        self,
        uploaded_after: arrow.Arrow | None,
        uploaded_until: arrow.Arrow | None = None,
        auto_batch: bool = False,
    ) -> None:
        # Inserts need the playlist; find out before spending quota on fetches or advancing the watermark. Dry runs
        # check too, so they surface a missing playlist instead of reporting a run that would fail.
        # This call also resolves `_credentials` on the main thread, before any `asyncio.to_thread` fan-out below:
        # `cached_property` has no lock, so a concurrent first resolution could run the OAuth flow more than once.
        self.get_watchlater_playlist()

        # Fetch only up to a point safely in the past and reuse it as the new watermark: the held-back window is
        # covered by the next run instead of being skipped (videos published mid-run, or listed late by YouTube).
        if uploaded_until is None:
            uploaded_until = arrow.now().shift(hours=-PUBLISH_DELAY_HOURS)

        channels = self.get_subscribed_channels()
        config = read_config()
        auto_add = config.setdefault('auto_add', [])

        if uploaded_after is None:
            if 'last_updated' in config:
                uploaded_after = arrow.get(config['last_updated'])
            else:
                uploaded_after = arrow.now().shift(weeks=-2)

        if uploaded_until <= uploaded_after:
            # Run again within the delay window (or --until before the watermark): nothing new is safe to fetch yet,
            # and writing uploaded_until would move the watermark backwards.
            print(f'Nothing to fetch before {uploaded_until}; last run already covered up to {uploaded_after}.')
            return

        stale = stale_channels(auto_add, channels)
        if stale:
            print(
                f"Skipping {len(stale)} allowlisted channel(s) you're no longer subscribed to: "
                f"{', '.join(i['name'] for i in stale)}. Run \"subscriptions remove\" to drop them."
            )

        allowed_channel_ids = {i['id'] for i in auto_add}
        allowed_channels = [i for i in channels if i['id'] in allowed_channel_ids]
        if not auto_add:
            print('No channels in the allowlist; run "subscriptions add" to add some.')
        all_videos = (
            asyncio.run(self.fetch_all_channels_videos(allowed_channels, uploaded_after, uploaded_until))
            if allowed_channels
            else []
        )

        effective_until = uploaded_until
        if auto_batch and len(all_videos) > MAX_INSERTS_PER_RUN:
            all_sorted_by_date = sorted(all_videos, key=lambda v: v['published_at'])
            effective_until = arrow.get(all_sorted_by_date[MAX_INSERTS_PER_RUN]['published_at'])
            all_videos = [v for v in all_videos if arrow.get(v['published_at']) < effective_until]
            remaining = len(all_sorted_by_date) - len(all_videos)
            print(
                f'Batch incomplete: queuing {len(all_videos)} of {len(all_sorted_by_date)} videos'
                f' through {effective_until}. {remaining} remaining.'
            )

        if all_videos:
            self.insert_videos_watch_later(all_videos)

        if not self.dry_run:
            config['last_updated'] = effective_until.format()
            write_config(config)

    def sort(self) -> None:
        """Sort the 'Sort Watch Later' playlist."""
        watchlater_id = self.get_watchlater_playlist()
        playlist_videos = self.get_playlist_videos(watchlater_id)

        if playlist_videos:
            self.sort_playlist(playlist_videos)
            # videos.list is only needed for durations; unavailable videos are simply absent from the total.
            self.print_duration(self.get_video_durations(playlist_videos))
        else:
            sys.exit(
                'Playlist is empty! '
                "Did you remember to copy over Youtube's Watch Later "
                'to your personal Sort Watch Later playlist?'
            )

    @staticmethod
    def print_duration(durations: dict[str, timedelta]) -> None:
        total_duration = sum(durations.values(), timedelta())
        print('\n' * 2)
        print(f'Total duration of playlist is {humanize_duration(total_duration)}')


@lru_cache(1)
def read_config() -> JsonType:
    if not CONFIG_FILE.exists():
        return {}

    with CONFIG_FILE.open('r', encoding='utf-8') as config:
        return yaml.safe_load(config) or {}


def write_config(config: JsonType) -> None:
    _write_atomically(
        CONFIG_FILE, lambda file: yaml.safe_dump(config, stream=file, explicit_start=True, default_flow_style=False)
    )


def save_token(credentials: Credentials) -> None:
    """Persist the token atomically, always owner-only (0600): it holds a full-scope refresh token."""
    _write_atomically(TOKEN_FILE, lambda file: file.write(credentials.to_json()), force_owner_only=True)
    if LEGACY_TOKEN_FILE.exists():
        print(f'{LEGACY_TOKEN_FILE} (the old oauth2client token) is no longer used; delete it.')


def _write_atomically(path: Path, write: Callable[[IO[str]], object], *, force_owner_only: bool = False) -> None:
    """Write via a temp file + rename, so an interrupted write never truncates `path`.

    `open(path, 'w')` empties the file the moment it opens; if the dump is then interrupted, the old contents are
    gone. Instead, write a temp file next to `path` (rename is only atomic within one filesystem) and rename it over
    `path` once complete. NamedTemporaryFile creates it 0600 with a unique name, so it is never readable by other
    users and cannot collide with another writer. An existing file's permissions are carried over (applied after
    writing, before the rename) so user changes such as group read survive; a new file stays 0600. Pass
    `force_owner_only=True` for a secrets file (e.g. the OAuth token) to always end up 0600, even if the existing
    file on disk was made more permissive. No fsync: power loss mid-write is out of scope (deliberate).

    Resolves `path` first so a symlinked config/state/token file (e.g. managed by stow or chezmoi) is written
    through the link instead of `os.replace` clobbering the link itself.
    """
    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode: int | None = 0o600 if force_owner_only else None
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = None
    with tempfile.NamedTemporaryFile(
        'w', encoding='utf-8', dir=path.parent, prefix=f'.{path.name}.', delete=False
    ) as file:
        try:
            write(file)
            file.flush()  # surface disk-full errors here, so the temp file is cleaned up
            if mode is not None:
                os.fchmod(file.fileno(), mode)
        except BaseException:
            os.unlink(file.name)
            raise
    os.replace(file.name, path)


app = typer.Typer(help='Tool to manage Youtube Watch Later playlist. Because they refuse to make it trivial.')


@app.callback()
def main(ctx: typer.Context, dry_run: bool = typer.Option(False, '--dry-run')) -> None:
    ctx.obj = dry_run


@app.command()
def sort(ctx: typer.Context) -> None:
    """Sort 'Watch Later' playlist."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.sort()


@app.command()
def update(
    ctx: typer.Context,
    since: str | None = typer.Option(None, '--since', help='Start date to filter videos by.'),
    until: str | None = typer.Option(None, '--until', help='End date to filter videos by.'),
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


subscriptions_app = typer.Typer(help='Manage channels allowed to auto-add videos.')
app.add_typer(subscriptions_app, name='subscriptions')


@subscriptions_app.command('add')
def subscriptions_add(ctx: typer.Context) -> None:
    """Interactively add newly-subscribed channels."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.add_subscriptions()


@subscriptions_app.command('list')
def subscriptions_list(
    ctx: typer.Context,
    check: bool = typer.Option(False, '--check', help='Also mark channels you are no longer subscribed to (signs in).'),
) -> None:
    """List channels currently allowed to auto-add videos."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.list_subscriptions(check)


@subscriptions_app.command('remove')
def subscriptions_remove(ctx: typer.Context) -> None:
    """Interactively remove channels from the auto-add list."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.remove_subscription()


if __name__ == '__main__':
    app()
