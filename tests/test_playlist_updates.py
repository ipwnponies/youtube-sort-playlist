import unittest
from unittest import mock

import playlist_updates
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


class UpdateTestCase(unittest.TestCase):
    """Base for update() tests: config I/O is patched, API-facing methods are fakes."""

    dry_run = False

    def setUp(self):
        self.config = {'auto_add': [{'id': 'c1', 'name': 'Channel'}], 'last_updated': '2026-01-01T00:00:00+00:00'}
        self.write_config = mock.Mock()
        for name, value in [('read_config', mock.Mock(return_value=self.config)), ('write_config', self.write_config)]:
            patcher = mock.patch.object(playlist_updates, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.manager, self.youtube = manager_with_fake_api(self.dry_run)
        fake_pages(self.youtube.playlists(), [{'id': 'wl', 'snippet': {'title': 'Sort Watch Later'}}])
        self.manager.get_subscribed_channels = mock.Mock(return_value=[{'id': 'c1', 'title': 'Channel'}])
        self.manager.insert_videos_watch_later = mock.Mock()

        self.fetched = []
        self.fetch_args = None

        async def fake_fetch(channels, uploaded_after, uploaded_until):
            self.fetch_args = (uploaded_after, uploaded_until)
            return list(self.fetched)

        self.manager.fetch_all_channels_videos = fake_fetch


class UpdateFailFastTest(UpdateTestCase):
    def test_missing_playlist_exits_before_fetching_or_writing(self):
        fake_pages(self.youtube.playlists(), [])

        with self.assertRaises(SystemExit):
            self.manager.update(None)

        self.manager.get_subscribed_channels.assert_not_called()
        self.write_config.assert_not_called()


class UpdateDryRunTest(UpdateTestCase):
    dry_run = True

    def test_dry_run_also_exits_when_playlist_missing(self):
        fake_pages(self.youtube.playlists(), [])

        with self.assertRaises(SystemExit):
            self.manager.update(None)

        self.manager.get_subscribed_channels.assert_not_called()

    def test_dry_run_with_playlist_writes_nothing(self):
        self.manager.update(None)

        self.write_config.assert_not_called()


if __name__ == '__main__':
    unittest.main()
