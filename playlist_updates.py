#! /usr/bin/env python
import os
import sys

import httplib2
from apiclient.discovery import build  # pylint: disable=import-error
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
""" % os.path.abspath(os.path.join(os.path.dirname(__file__),
                                   CLIENT_SECRETS_FILE))

# This OAuth 2.0 access scope allows for full read/write access to the
# authenticated user's account.
YOUTUBE_READ_WRITE_SCOPE = 'https://www.googleapis.com/auth/youtube'
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'


def get_watchlater_playlist(youtube):
    playlists = youtube.playlists().list(part='snippet', mine=True).execute()
    for playlist in playlists['items']:
        if playlist['snippet']['title'] == 'Watch Later':
            return playlist['id']


def get_playlist_videos(youtube, watchlater_id):
    '''Returns list of tuples containing the video id and position in Watch Later playlist'''
    result = []

    request = youtube.playlistItems().list(
        part='snippet',
        playlistId=watchlater_id,
        maxResults=50
    )

    # Iterate through all reuslts pages
    while request:
        response = request.execute()

        result.extend(response['items'])

        # Prepare next results page
        request = youtube.playlistItems().list_next(request, response)
    return result


def get_channel(youtube, videos):
    '''Takes a list of video ids and returns the channel information'''
    result = {}

    # Partition videos due to max number of videos queryable with one api call
    while videos:
        to_query = videos[:50]
        remaining = videos[50:]

        response = youtube.videos().list(
            part='snippet',
            id=','.join(list(to_query)),
            maxResults=50
        ).execute()

        for i in response['items']:
            video_id = i['id']
            channel_id = i['snippet']['channelId']
            result[video_id] = channel_id

        videos = remaining

    return result


def sort_playlist(youtube, playlist_videos):
    video_ids = [i['snippet']['resourceId']['videoId']
                 for i in playlist_videos]
    channel_map = get_channel(youtube, video_ids)

    def sorter(val):
        video_id = val['snippet']['resourceId']['videoId']
        return channel_map[video_id]

    sorted_playlist = sorted(playlist_videos, key=sorter)
    for index, i in enumerate(sorted_playlist):
        i['snippet']['position'] = index
        print('{} is being put in pos {}'.format(i['snippet']['title'], index))
        youtube.playlistItems().update(part='snippet', body=i).execute()


def get_creds():
    '''Authorize client with OAuth2.'''
    flow = flow_from_clientsecrets(
        CLIENT_SECRETS_FILE,
        message=MISSING_CLIENT_SECRETS_MESSAGE,
        scope=YOUTUBE_READ_WRITE_SCOPE
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
        http=creds.authorize(httplib2.Http())
    )


def main():
    youtube = get_youtube()
    watchlater_id = get_watchlater_playlist(youtube)
    if not watchlater_id:
        exit('Oh noes, you don\'t have a playlist named Watch Later')
    playlist_videos = get_playlist_videos(youtube, watchlater_id)
    sort_playlist(youtube, playlist_videos)


if __name__ == '__main__':
    exit(main())
