#! /usr/bin/env python
import asyncio
import bisect
import os
import stat
import sys
import tempfile
import threading
from collections import namedtuple
from datetime import timedelta
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

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
from isodate import parse_duration
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from tqdm import tqdm
from xdg import xdg_cache_home

print = tqdm.write


# The CLIENT_SECRETS_FILE variable specifies the name of a file that contains
# the OAuth 2.0 information for this application, including its client_id and
# client_secret. You can acquire an OAuth 2.0 client ID and client secret from
# the {{ Google Cloud Console }} at
# {{ https://cloud.google.com/console }}.
# Please ensure that you have enabled the YouTube Data API for your project.
# For more information about using OAuth2 to access the YouTube Data API, see:
#   https://developers.google.com/youtube/v3/guides/authentication
# For more information about the client_secrets.json file format, see:
#   https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
CLIENT_SECRETS_FILE = 'client_secrets.json'

# This variable defines a message to display if the CLIENT_SECRETS_FILE is
# missing.
MISSING_CLIENT_SECRETS_MESSAGE = f"""
WARNING: Please configure OAuth 2.0

To make this sample run you will need to populate the client_secrets.json file
found at:

   {os.path.abspath(os.path.join(os.path.dirname(__file__), CLIENT_SECRETS_FILE))}

with information from the {{{{ Cloud Console }}}}
{{{{ https://cloud.google.com/console }}}}

For more information about the client_secrets.json file format, please visit:
https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
"""

APP_NAME = 'youtube-sort-playlist'
CONFIG_FILE = xdg_cache_home() / APP_NAME / 'config.yaml'

# This OAuth 2.0 access scope allows for full read/write access to the
# authenticated user's account.
YOUTUBE_READ_WRITE_SCOPE = 'https://www.googleapis.com/auth/youtube'
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'

SORT_PLAYLIST_TITLE = 'Sort Watch Later'

DAILY_QUOTA = 10_000
INSERT_COST = 50
MAX_INSERTS_PER_RUN = int(DAILY_QUOTA * 0.8 / INSERT_COST)  # 160
# Newest uploads are left for the next run: covers videos published mid-run and YouTube listing uploads late.
PUBLISH_DELAY_HOURS = 3

VideoInfo = namedtuple('VideoInfo', ['channel_id', 'published_date', 'duration'])
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


class YoutubeManager:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self._thread_local = threading.local()

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

    @cached_property
    def _credentials(self) -> oauth2client.client.Credentials:
        """Lazily-resolved OAuth credentials.

        Only commands that actually hit the YouTube API need these; local-file-only commands (e.g.
        `subscriptions list`/`remove`) must not trigger an OAuth flow just to construct a manager.
        """
        return self.get_creds()

    @property
    def youtube(self):
        """Thread-local youtube data v3 object.

        httplib2.Http is not thread-safe: it keeps a single per-host connection cache, so sharing one
        instance across the threads used for concurrent channel fetches/inserts causes requests to
        interleave on the same socket (hangs, or worse, native heap corruption). Each thread lazily
        builds and keeps its own client.
        """
        if not hasattr(self._thread_local, 'youtube'):
            self._thread_local.youtube = build(
                YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, http=self._credentials.authorize(httplib2.Http())
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

    def get_video_info(self, playlist_videos: list[JsonType]) -> dict[str, VideoInfo]:
        """Returns a dict of VideoInfo for each video

        The key is video id and the value is VideoInfo.
        """
        result = {}
        videos = [i['snippet']['resourceId']['videoId'] for i in playlist_videos]

        # Partition videos due to max number of videos queryable with one api call
        while videos:
            to_query = videos[:50]
            remaining = videos[50:]

            response = (
                self.youtube.videos()
                .list(part='snippet,contentDetails', id=','.join(list(to_query)), maxResults=50)
                .execute()
            )

            for i in response['items']:
                video_id = i['id']
                channel_id = i['snippet']['channelId']
                published_date = i['snippet']['publishedAt']
                duration = parse_duration(i['contentDetails']['duration'])
                result[video_id] = VideoInfo(channel_id, published_date, duration)

            videos = remaining

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
        next_page_token = None
        request = self.youtube.subscriptions().list(part='snippet', mine=True, maxResults=50, pageToken=next_page_token)

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

    def list_subscriptions(self) -> None:
        """Print the channels currently allowed to auto-add videos."""
        config = read_config()
        auto_add = config.get('auto_add', [])

        if not auto_add:
            print('No subscriptions.')
            return

        table = Table('Name', 'Channel ID')
        for channel in auto_add:
            table.add_row(escape(channel['name']), escape(channel['id']))

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

    def add_video_to_watch_later(self, video_id: JsonType) -> None:
        print(f"Adding video to playlist: {video_id['title']}")
        if not self.dry_run:
            try:
                self.youtube.playlistItems().insert(
                    part='snippet',
                    body={
                        'snippet': {
                            'playlistId': self.get_watchlater_playlist(),
                            'resourceId': {'kind': 'youtube#video', 'videoId': video_id['id']},
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

        allowed_channel_ids = {i['id'] for i in auto_add}
        allowed_channels = [i for i in channels if i['id'] in allowed_channel_ids]
        if not allowed_channels:
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
            self.print_duration(self.get_video_info(playlist_videos))
        else:
            sys.exit(
                'Playlist is empty! '
                "Did you remember to copy over Youtube's Watch Later "
                'to your personal Sort Watch Later playlist?'
            )

    @staticmethod
    def print_duration(video_infos: JsonType) -> None:
        total_duration = sum((video.duration for video in video_infos.values()), timedelta())
        print('\n' * 2)
        print(f'Total duration of playlist is {humanize_duration(total_duration)}')


@lru_cache(1)
def read_config() -> JsonType:
    if not CONFIG_FILE.exists():
        return {}

    with CONFIG_FILE.open('r', encoding='utf-8') as config:
        return yaml.safe_load(config) or {}


def write_config(config: JsonType) -> None:
    _write_yaml_atomically(CONFIG_FILE, config)


def _write_yaml_atomically(path: Path, data: JsonType) -> None:
    """Write via a temp file + rename, so an interrupted write never truncates `path`.

    `open(path, 'w')` empties the file the moment it opens; if the dump is then interrupted, the old contents are
    gone. Instead, write a temp file next to `path` (rename is only atomic within one filesystem) and rename it over
    `path` once complete. NamedTemporaryFile creates it 0600 with a unique name, so it is never readable by other
    users and cannot collide with another writer. An existing file's permissions are carried over (applied after
    writing, before the rename) so user changes such as group read survive; a new file stays 0600. No fsync: power
    loss mid-write is out of scope (deliberate).
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    with tempfile.NamedTemporaryFile(
        'w', encoding='utf-8', dir=path.parent, prefix=f'.{path.name}.', delete=False
    ) as file:
        try:
            yaml.safe_dump(data, stream=file, explicit_start=True, default_flow_style=False)
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
def subscriptions_list(ctx: typer.Context) -> None:
    """List channels currently allowed to auto-add videos."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.list_subscriptions()


@subscriptions_app.command('remove')
def subscriptions_remove(ctx: typer.Context) -> None:
    """Interactively remove channels from the auto-add list."""
    youtube_manager = YoutubeManager(ctx.obj)
    youtube_manager.remove_subscription()


if __name__ == '__main__':
    app()
