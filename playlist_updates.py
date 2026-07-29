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
from rich.markup import escape
from rich.table import Table
from tqdm import tqdm
from xdg import XDG_CACHE_HOME

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
MISSING_CLIENT_SECRETS_MESSAGE = """
WARNING: Please configure OAuth 2.0

To make this sample run you will need to populate the client_secrets.json file
found at:

   %s

with information from the {{ Cloud Console }}
{{ https://cloud.google.com/console }}

For more information about the client_secrets.json file format, please visit:
https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
""" % os.path.abspath(os.path.join(os.path.dirname(__file__), CLIENT_SECRETS_FILE))

# This OAuth 2.0 access scope allows for full read/write access to the
# authenticated user's account.
YOUTUBE_READ_WRITE_SCOPE = 'https://www.googleapis.com/auth/youtube'
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'

DAILY_QUOTA = 10_000
INSERT_COST = 50
MAX_INSERTS_PER_RUN = int(DAILY_QUOTA * 0.8 / INSERT_COST)  # 160

VideoInfo = namedtuple('VideoInfo', ['channel_id', 'published_date', 'duration'])
JsonType = Dict[str, Any]


class YoutubeManager:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self._credentials = self.get_creds()
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
        youtube users have by default.
        """
        playlists = self.youtube.playlists().list(part='snippet', mine=True).execute()
        playlist_id = next(i['id'] for i in playlists['items'] if i['snippet']['title'] == 'Sort Watch Later')
        return playlist_id

    def get_playlist_videos(self, watchlater_id: str) -> List[JsonType]:
        """Returns list of playlistItems from Sort Watch Later playlist"""
        result: List[Dict] = []

        request = self.youtube.playlistItems().list(part='snippet', playlistId=watchlater_id, maxResults=50)

        # Iterate through all results pages
        while request:
            response: Dict[str, Dict] = request.execute()

            result.extend(response['items'])

            # Prepare next results page
            request = self.youtube.playlistItems().list_next(request, response)
        return result

    def get_video_info(self, playlist_videos: List[JsonType]) -> Dict[str, VideoInfo]:
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

    def sort_playlist(self, playlist_videos: List[Dict], video_infos: JsonType) -> None:
        """Sorts a playlist and groups videos by channel."""

        def sort_key(playlist_item):
            """Groups together videos from the same channel, sorted by date in ascending order."""
            video_id = playlist_item['snippet']['resourceId']['videoId']
            channel_name, published_date, _ = video_infos[video_id]
            return f'{channel_name}-{published_date}'

        sorted_playlist = sorted(playlist_videos, key=sort_key)
        for index, i in enumerate(tqdm(sorted_playlist, unit='video')):
            print(f"{i['snippet']['title']} is being put in pos {index}")

            if not self.dry_run:
                i['snippet']['position'] = index
                self.youtube.playlistItems().update(part='snippet', body=i).execute()

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

    def get_channel_details(self, channel_id: str) -> addict.Dict:
        request = self.youtube.channels().list(part='contentDetails', id=channel_id)

        # Only 1 item, since queried by id
        channel_details = addict.Dict(request.execute()['items'][0])
        return channel_details

    def fetch_channel_videos(
        self, channel: str, uploaded_after: arrow.Arrow, uploaded_until: Optional[arrow.Arrow] = None
    ) -> List[JsonType]:
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
        self, channels: List[Dict[str, str]], uploaded_after: arrow.Arrow, uploaded_until: Optional[arrow.Arrow]
    ) -> List[JsonType]:
        """Fetch each channel's recent videos concurrently.

        Fetching is a pure read with no ordering requirement, so channels are processed in parallel. A failure on
        any channel aborts the whole batch: a partial channel set must never reach the insert phase, since that
        would let `last_updated` advance past videos we never actually looked at.
        """
        tasks = [
            asyncio.to_thread(self.fetch_channel_videos, channel['id'], uploaded_after, uploaded_until)
            for channel in channels
        ]

        all_videos: List[JsonType] = []
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

    def insert_videos_watch_later(self, videos: List[JsonType]) -> None:
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
            config['last_updated'] = effective_until.format() if effective_until else arrow.now().format()
            write_config(config)

    def sort(self) -> None:
        """Sort the 'Sort Watch Later' playlist."""
        watchlater_id = self.get_watchlater_playlist()
        if not watchlater_id:
            sys.exit("Oh noes, you don't have a playlist named Sort Watch Later")

        playlist_videos = self.get_playlist_videos(watchlater_id)

        if playlist_videos:
            video_infos = self.get_video_info(playlist_videos)
            self.sort_playlist(playlist_videos, video_infos)
            self.print_duration(video_infos)
        else:
            sys.exit(
                'Playlist is empty! '
                "Did you remember to copy over Youtube's Watch Later "
                'to your personal Sort Watch Later playlist?'
            )

    @staticmethod
    def print_duration(video_infos: JsonType) -> None:
        total_duration = reduce(operator.add, [video.duration for video in video_infos.values()])
        print('\n' * 2)
        print(f"Total duration of playlist is {strftime(total_duration, '%H:%M')}")


@lru_cache(1)
def read_config() -> JsonType:
    config_dir = Path(XDG_CACHE_HOME) / 'youtube-sort-playlist'
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = config_dir / 'config.yaml'
    config_file.touch()

    with config_file.open('r') as config:
        return yaml.safe_load(config) or {}


def write_config(config: JsonType) -> None:
    with open(os.path.join(XDG_CACHE_HOME, 'youtube-sort-playlist', 'config.yaml'), 'w', encoding='utf-8') as file:
        yaml.safe_dump(config, stream=file, explicit_start=True, default_flow_style=False)


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


if __name__ == '__main__':
    app()
