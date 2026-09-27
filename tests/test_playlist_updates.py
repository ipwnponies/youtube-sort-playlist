import random
import unittest
from datetime import timedelta
from unittest import mock

import playlist_updates
from playlist_updates import VideoInfo, YoutubeManager, plan_moves


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


def playlist_item(item_id, video_id):
    return {'id': item_id, 'snippet': {'title': video_id, 'resourceId': {'videoId': video_id}}}


def apply_moves(current, moves):
    """Simulate playlistItems.update position semantics: take the item out, reinsert at the position."""
    order = list(current)
    for item_id, position in moves:
        order.remove(item_id)
        order.insert(position, item_id)
    return order


def lis_length(values):
    """Deliberately naive O(n^2) DP, independent of the O(n log n) code under test, used as an oracle.

    The minimum number of moves is len(values) minus this.
    """
    best = [1] * len(values)
    for i in range(len(values)):
        for j in range(i):
            if values[j] < values[i]:
                best[i] = max(best[i], best[j] + 1)
    return max(best, default=0)


def moves_sent(youtube):
    """(item id, position) of every playlistItems.update call, in call order."""
    return [
        (call.kwargs['body']['id'], call.kwargs['body']['snippet']['position'])
        for call in youtube.playlistItems().update.call_args_list
    ]


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


class PlanMovesTest(unittest.TestCase):
    def test_sorted_playlist_needs_no_moves(self):
        self.assertEqual(plan_moves(['a', 'b', 'c'], ['a', 'b', 'c']), [])

    def test_single_misplaced_item_moves_once(self):
        self.assertEqual(plan_moves(['z', 'a', 'b', 'c'], ['a', 'b', 'c', 'z']), [('z', 3)])

    def test_random_permutations_reach_target_in_fewest_moves(self):
        rng = random.Random(0)
        for size in range(0, 40):
            target = [f'item{i}' for i in range(size)]
            current = target[:]
            rng.shuffle(current)

            moves = plan_moves(current, target)

            self.assertEqual(apply_moves(current, moves), target)
            self.assertEqual(len(moves), size - lis_length([target.index(i) for i in current]))


class SortPlaylistTest(unittest.TestCase):
    def test_only_out_of_place_items_are_updated(self):
        manager, youtube = manager_with_fake_api()
        items = [playlist_item('p1', 'v1'), playlist_item('p2', 'v2'), playlist_item('p3', 'v3')]
        infos = {
            'v1': VideoInfo('chanA', '2026-01-02', timedelta()),
            'v2': VideoInfo('chanA', '2026-01-03', timedelta()),
            'v3': VideoInfo('chanA', '2026-01-01', timedelta()),
        }

        manager.sort_playlist(items, infos)

        self.assertEqual(moves_sent(youtube), [('p3', 0)])

    def test_same_video_twice_is_handled_per_playlist_item(self):
        manager, youtube = manager_with_fake_api()
        items = [playlist_item('p1', 'v2'), playlist_item('p2', 'v1'), playlist_item('p3', 'v2')]
        infos = {
            'v1': VideoInfo('chanA', '2026-01-01', timedelta()),
            'v2': VideoInfo('chanA', '2026-01-02', timedelta()),
        }

        manager.sort_playlist(items, infos)

        moves = moves_sent(youtube)
        self.assertEqual(len(moves), 1)
        self.assertEqual(apply_moves(['p1', 'p2', 'p3'], moves), ['p2', 'p1', 'p3'])

    def test_dry_run_does_not_update(self):
        manager, youtube = manager_with_fake_api(dry_run=True)
        items = [playlist_item('p1', 'v1'), playlist_item('p2', 'v2')]
        infos = {
            'v1': VideoInfo('chanB', '2026-01-01', timedelta()),
            'v2': VideoInfo('chanA', '2026-01-01', timedelta()),
        }

        manager.sort_playlist(items, infos)

        youtube.playlistItems().update.assert_not_called()


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
