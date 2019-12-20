#! /usr/bin/env python
import argparse
import operator
import os
import sys
from collections import namedtuple
from functools import lru_cache
from functools import reduce
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List

import addict
import arrow
import googleapiclient.errors
import httplib2
import oauth2client.client
import oauth2client.file
import oauth2client.tools
import yaml
from apiclient.discovery import build  # pylint: disable=import-error
from isodate import parse_duration
from isodate import strftime
from tqdm import tqdm
from xdg import XDG_CACHE_HOME


print = tqdm.write  # pylint: disable=invalid-name,redefined-builtin


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
""" % os.path.abspath(
    os.path.join(os.path.dirname(__file__), CLIENT_SECRETS_FILE)
)

# This OAuth 2.0 access scope allows for full read/write access to the
# authenticated user's account.
YOUTUBE_READ_WRITE_SCOPE = 'https://www.googleapis.com/auth/youtube'
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'

VideoInfo = namedtuple('VideoInfo', ['channel_id', 'published_date', 'duration'])
JsonType = Dict[str, Any]


class YoutubeManager:
    def __init__(self, dry_run: bool, args: List[str]) -> None:
        self.youtube = self.get_youtube(args)
        self.dry_run = dry_run

    @staticmethod
    def get_creds(args: List[str]) -> oauth2client.client.Credentials:
        '''Authorize client with OAuth2.'''
        flow = oauth2client.client.flow_from_clientsecrets(
            CLIENT_SECRETS_FILE, message=MISSING_CLIENT_SECRETS_MESSAGE, scope=YOUTUBE_READ_WRITE_SCOPE
        )

        storage = oauth2client.file.Storage('{}-oauth2.json'.format(sys.argv[0]))
        credentials = storage.get()

        if credentials is None or credentials.invalid:
            flags = oauth2client.tools.argparser.parse_args(args)
            credentials = oauth2client.tools.run_flow(flow, storage, flags)

        return credentials

    def get_youtube(self, args: List[str]):
        '''Get youtube data v3 object.'''
        creds = self.get_creds(args)
        return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, http=creds.authorize(httplib2.Http()))

    def get_watchlater_playlist(self) -> str:
        '''Get the id of the 'Sort Watch Later' playlist.

        The 'Sort Watch Later' playlist is regular playlist and is not the same as the magical one that all
        youtube users have by default.
        '''
        playlists = self.youtube.playlists().list(part='snippet', mine=True).execute()
        playlist_id = next(i['id'] for i in playlists['items'] if i['snippet']['title'] == 'Sort Watch Later')
        return playlist_id

    def get_playlist_videos(self, watchlater_id: str) -> List[JsonType]:
        '''Returns list of playlistItems from Sort Watch Later playlist'''
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
        '''Returns a dict of VideoInfo for each video

        The key is video id and the value is VideoInfo.
        '''
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
        '''Sorts a playlist and groups videos by channel.'''

        def sort_key(playlist_item):
            '''Groups together videos from the same channel, sorted by date in ascending order.'''
            video_id = playlist_item['snippet']['resourceId']['videoId']
            channel_name, published_date, _ = video_infos[video_id]
            return '{}-{}'.format(channel_name, published_date)

        sorted_playlist = sorted(playlist_videos, key=sort_key)
        for index, i in enumerate(tqdm(sorted_playlist, unit='video')):
            print('{} is being put in pos {}'.format(i['snippet']['title'], index))

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

    def get_channel_details(self, channel_id: str) -> addict.Dict:
        request = self.youtube.channels().list(part='contentDetails', id=channel_id)

        # Only 1 item, since queried by id
        channel_details = addict.Dict(request.execute()['items'][0])
        return channel_details

    def add_channel_videos_watch_later(self, channel: str, uploaded_after: arrow) -> None:
        video_ids = []

        channel_details = self.get_channel_details(channel)
        uploaded_playlist = channel_details.contentDetails.relatedPlaylists.uploads

        request = self.youtube.playlistItems().list(part='snippet', playlistId=uploaded_playlist, maxResults=50)

        while request:
            response = addict.Dict(request.execute())
            recent_videos = [
                {'id': i.snippet.resourceId.videoId, 'title': i.snippet.title}
                for i in response['items']
                if i.snippet.resourceId.kind == 'youtube#video' and arrow.get(i.snippet.publishedAt) >= uploaded_after
            ]

            if not recent_videos:
                break

            video_ids.extend(recent_videos)
            request = self.youtube.playlistItems().list_next(request, response)

        for video_id in video_ids:
            self.add_video_to_watch_later(video_id)

    def add_video_to_watch_later(self, video_id: JsonType) -> None:
        print('Adding video to playlist: {}'.format(video_id['title']))
        if not self.dry_run:
            try:
                self.youtube.playlistItems().insert(
                    part='snippet',
                    body={
                        'snippet': {
                            'playlistId': 'WL',
                            'resourceId': {'kind': 'youtube#video', 'videoId': video_id['id']},
                        }
                    },
                ).execute()
            except googleapiclient.errors.HttpError as error:
                if error.resp.status == 409:
                    print('Already in list, skipping!')
                else:
                    raise

    def update(self, uploaded_after: arrow, only_allowed: bool = False) -> None:
        channels = self.get_subscribed_channels()
        config = read_config()
        auto_add = config.setdefault('auto_add', [])

        if uploaded_after is None:
            if 'last_updated' in config:
                uploaded_after = arrow.get(config['last_updated'])
            else:
                uploaded_after = arrow.now().shift(weeks=-2)

        if not only_allowed and not self.dry_run:
            unknown_channels = [i for i in channels if i['id'] not in auto_add]
            for channel in unknown_channels:
                response = input('Want to auto-add videos from "{}"? y/n: '.format(channel['title']))
                if response == 'y':
                    auto_add.append(channel['id'])
            write_config(config)

        allowed_channels = [i for i in channels if i['id'] in auto_add]
        for channel in tqdm(allowed_channels, unit='video'):
            self.add_channel_videos_watch_later(channel['id'], uploaded_after)

        if not self.dry_run:
            config['last_updated'] = arrow.now().format()
            write_config(config)

    def sort(self) -> None:
        '''Sort the 'Sort Watch Later' playlist.'''
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
        print('Total duration of playlist is {}'.format(strftime(total_duration, '%H:%M')))


@lru_cache(1)
def read_config() -> JsonType:
    config_dir = Path(XDG_CACHE_HOME) / 'youtube-sort-playlist'
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = config_dir / 'config.yaml'
    config_file.touch()

    with config_file.open('r') as config:
        return yaml.safe_load(config) or {}


def write_config(config: JsonType) -> None:
    with open(os.path.join(XDG_CACHE_HOME, 'youtube-sort-playlist', 'config.yaml'), 'w') as file:
        yaml.safe_dump(config, stream=file, explicit_start=True, default_flow_style=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Tool to manage Youtube Watch Later playlist. Because they refuse to make it trivial.'
    )
    parser.add_argument('args', nargs=argparse.REMAINDER)

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument('--dry-run', action='store_true')

    subparser = parser.add_subparsers(title='sub-commands', dest='subcommand')
    subparser.add_parser(
        'sort',
        help="Sort 'Watch Later' playlist.",
        description="Sort the 'Sort Watch Later' playlist and group by channel.",
        parents=[common_parser],
    )

    update_parser = subparser.add_parser(
        'update',
        help='Add recent videos to watch later playlist.',
        description='Update the watch later playlist with recent videos from subscribed channels.',
        parents=[common_parser],
    )
    update_parser.add_argument('--since', help='Start date to filter videos by.', type=arrow.get)
    update_parser.add_argument(
        '-f', '--only-allowed', help='Auto add videos from known and allowed channels.', action='store_true'
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    youtube_manager = YoutubeManager(args.dry_run, args.args)
    if args.subcommand == 'sort':
        youtube_manager.sort()
    elif args.subcommand == 'update':
        youtube_manager.update(args.since, args.only_allowed)


if __name__ == '__main__':
    main()
