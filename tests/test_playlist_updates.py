import unittest
from unittest import mock

from playlist_updates import YoutubeManager


def manager_with_fake_api(dry_run=False):
    """A manager whose YouTube client is a MagicMock, bypassing OAuth and the network."""
    manager = YoutubeManager(dry_run)
    manager._thread_local.youtube = mock.MagicMock()
    return manager, manager._thread_local.youtube


def fake_pages(resource, *pages):
    """Make `resource.list(...)` then `resource.list_next(...)` yield `pages` in order, then stop."""
    requests = [mock.MagicMock(**{'execute.return_value': {'items': list(page)}}) for page in pages]
    resource.list.return_value = requests[0]
    resource.list_next.side_effect = requests[1:] + [None]


class GetWatchlaterPlaylistTest(unittest.TestCase):
    def test_finds_playlist_beyond_first_page(self):
        manager, youtube = manager_with_fake_api()
        fake_pages(
            youtube.playlists(),
            [{'id': 'other', 'snippet': {'title': 'Other'}}],
            [{'id': 'target', 'snippet': {'title': 'Sort Watch Later'}}],
        )

        self.assertEqual(manager.get_watchlater_playlist(), 'target')
        youtube.playlists().list.assert_called_once_with(part='snippet', mine=True, maxResults=50)

    def test_missing_playlist_exits_with_message(self):
        manager, youtube = manager_with_fake_api()
        fake_pages(youtube.playlists(), [{'id': 'other', 'snippet': {'title': 'Other'}}])

        with self.assertRaises(SystemExit) as context:
            manager.get_watchlater_playlist()
        self.assertIn('Sort Watch Later', str(context.exception.code))


if __name__ == '__main__':
    unittest.main()
