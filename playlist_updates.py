#! /usr/bin/env python
import operator
import os
import sys
from collections import namedtuple
from functools import reduce

import addict
import httplib2
from apiclient.discovery import build  # pylint: disable=import-error
from isodate import parse_duration
from isodate import strftime
from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage
from oauth2client.tools import argparser
from oauth2client.tools import run_flow


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
""" % os.path.abspath(os.path.join(os.path.dirname(__file__), CLIENT_SECRETS_FILE,))

# This OAuth 2.0 access scope allows for full read/write access to the
# authenticated user's account.
YOUTUBE_READ_WRITE_SCOPE = 'https://www.googleapis.com/auth/youtube'
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'

VideoInfo = namedtuple('VideoInfo', ['channel_id', 'published_date', 'duration'])


def get_watchlater_playlist(youtube):
    '''Get the id of the 'Sort Watch Later' playlist.

    The 'Sort Watch Later' playlist is regular playlist and is not the same as the magical one that all
    youtube users have by default.
    '''
    playlists = youtube.playlists().list(part='snippet', mine=True).execute()
    playlist_id = next(i['id'] for i in playlists['items'] if i['snippet']['title'] == 'Sort Watch Later')
    return playlist_id


def get_playlist_videos(youtube, watchlater_id):
    '''Returns list of playlistItems from Sort Watch Later playlist'''
    result = []

    request = youtube.playlistItems().list(
        part='snippet',
        playlistId=watchlater_id,
        maxResults=50,
    )

    # Iterate through all results pages
    while request:
        response = request.execute()

        result.extend(response['items'])

        # Prepare next results page
        request = youtube.playlistItems().list_next(request, response)
    return result


def get_video_info(youtube, playlist_videos):
    '''Returns a dict of VideoInfo for each video

    The key is video id and the value is VideoInfo.
    '''
    result = {}
    videos = [i['snippet']['resourceId']['videoId'] for i in playlist_videos]

    # Partition videos due to max number of videos queryable with one api call
    while videos:
        to_query = videos[:50]
        remaining = videos[50:]

        response = youtube.videos().list(
            part='snippet,contentDetails',
            id=','.join(list(to_query)),
            maxResults=50,
        ).execute()

        for i in response['items']:
            video_id = i['id']
            channel_id = i['snippet']['channelId']
            published_date = i['snippet']['publishedAt']
            duration = parse_duration(i['contentDetails']['duration'])
            result[video_id] = VideoInfo(channel_id, published_date, duration)

        videos = remaining

    return result


def sort_playlist(youtube, playlist_videos, video_infos):
    '''Sorts a playlist and groups videos by channel.'''

    def sort_key(playlist_item):
        '''Groups together videos from the same channel, sorted by date in ascending order.'''
        video_id = playlist_item['snippet']['resourceId']['videoId']
        channel_name, published_date, _ = video_infos[video_id]
        return '{}-{}'.format(channel_name, published_date)

    sorted_playlist = sorted(playlist_videos, key=sort_key)
    for index, i in enumerate(sorted_playlist):
        i['snippet']['position'] = index
        print('{} is being put in pos {}'.format(i['snippet']['title'], index))
        youtube.playlistItems().update(part='snippet', body=i).execute()


def print_duration(video_infos):
    total_duration = reduce(operator.add, [video.duration for video in video_infos.values()])
    print('\n' * 2)
    print('Total duration of playlist is {}'.format(strftime(total_duration, '%H:%M')))


def get_creds():
    '''Authorize client with OAuth2.'''
    flow = flow_from_clientsecrets(
        CLIENT_SECRETS_FILE,
        message=MISSING_CLIENT_SECRETS_MESSAGE,
        scope=YOUTUBE_READ_WRITE_SCOPE,
    )

    storage = Storage('%s-oauth2.json' % sys.argv[0])
    credentials = storage.get()

    if credentials is None or credentials.invalid:
        flags = argparser.parse_args()
        credentials = run_flow(flow, storage, flags)

    return credentials


def get_youtube():
    '''Get youtube data v3 object.'''
    creds = get_creds()
    return build(
        YOUTUBE_API_SERVICE_NAME,
        YOUTUBE_API_VERSION,
        http=creds.authorize(httplib2.Http()),
    )


def get_subscribed_channels(youtube,):
    subscriptions = youtube.subscriptions().list(part='snippet', mine=True).execute()
    subscriptions = addict.Dict(subscriptions)
    channels = [
        {'title': i.snippet.title, 'id': i.snippet.resourceId.channelId}
        for i in subscriptions['items']
    ]
    return channels


def add_channel_videos_watch_later(youtube, channel, uploaded_after):
    recent_videos = youtube.search().list(
        part='snippet',
        channelId=channel,
        type='video',
        publishedAfter=uploaded_after,
    ).execute()

    video_ids = [i.id.videoId for i in addict.Dict(recent_videos)['items']]
    for video_id in video_ids:
        add_video_to_watch_later(youtube, video_id)


def add_video_to_watch_later(youtube, video_id):
    youtube.playlistItems().insert(
        part='snippet',
        body={
            'snippet': {
                'playlistId': 'WL',
                'resourceId': {
                    'kind': 'youtube#video',
                    'videoId': video_id,
                },
            },
        },
    ).execute()


def main():
    '''Execute the main script to sort Sort Watch Later playlist.'''
    youtube = get_youtube()
    watchlater_id = get_watchlater_playlist(youtube)
    if not watchlater_id:
        exit('Oh noes, you don\'t have a playlist named Sort Watch Later')

    playlist_videos = get_playlist_videos(youtube, watchlater_id)

    if playlist_videos:
        video_infos = get_video_info(youtube, playlist_videos)
        sort_playlist(youtube, playlist_videos, video_infos)
        print_duration(video_infos)
    else:
        exit(
            'Playlist is empty! '
            'Did you remember to copy over Youtube\'s Watch Later '
            'to your personal Sort Watch Later playlist?',
        )


if __name__ == '__main__':
    exit(main())
