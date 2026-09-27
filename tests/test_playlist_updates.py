import io
import random
import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import arrow
import yaml
from rich.console import Console

import playlist_updates
from playlist_updates import YoutubeManager, plan_moves


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


def playlist_item(item_id, video_id, channel_id=None, published_at=None):
    """A playlistItems.list entry. Omit channel_id/published_at to model a deleted or private video."""
    item = {
        'id': item_id,
        'snippet': {'title': video_id, 'resourceId': {'videoId': video_id}},
        'contentDetails': {'videoId': video_id},
    }
    if channel_id is not None:
        item['snippet']['videoOwnerChannelId'] = channel_id
        item['contentDetails']['videoPublishedAt'] = published_at
    return item


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
        items = [
            playlist_item('p1', 'v1', 'chanA', '2026-01-02'),
            playlist_item('p2', 'v2', 'chanA', '2026-01-03'),
            playlist_item('p3', 'v3', 'chanA', '2026-01-01'),
        ]

        manager.sort_playlist(items)

        self.assertEqual(moves_sent(youtube), [('p3', 0)])

    def test_same_video_twice_is_handled_per_playlist_item(self):
        manager, youtube = manager_with_fake_api()
        items = [
            playlist_item('p1', 'v2', 'chanA', '2026-01-02'),
            playlist_item('p2', 'v1', 'chanA', '2026-01-01'),
            playlist_item('p3', 'v2', 'chanA', '2026-01-02'),
        ]

        manager.sort_playlist(items)

        moves = moves_sent(youtube)
        self.assertEqual(len(moves), 1)
        self.assertEqual(apply_moves(['p1', 'p2', 'p3'], moves), ['p2', 'p1', 'p3'])

    def test_dry_run_does_not_update(self):
        manager, youtube = manager_with_fake_api(dry_run=True)
        items = [playlist_item('p1', 'v1', 'chanB', '2026-01-01'), playlist_item('p2', 'v2', 'chanA', '2026-01-01')]

        manager.sort_playlist(items)

        youtube.playlistItems().update.assert_not_called()

    def test_unavailable_entries_move_to_front_before_anything_else(self):
        manager, youtube = manager_with_fake_api()
        items = [
            playlist_item('p1', 'v1', 'chanA', '2026-01-02'),
            playlist_item('p2', 'v2', 'chanA', '2026-01-01'),
            playlist_item('p3', 'v3', 'chanA', '2026-01-03'),
            playlist_item('p4', 'deleted'),
        ]

        manager.sort_playlist(items)

        moves = moves_sent(youtube)
        self.assertEqual(moves, [('p4', 0), ('p1', 2)])
        self.assertEqual(apply_moves(['p1', 'p2', 'p3', 'p4'], moves), ['p4', 'p2', 'p1', 'p3'])

    def test_second_sort_makes_no_moves(self):
        manager, youtube = manager_with_fake_api()
        items = [
            playlist_item('p4', 'deleted'),
            playlist_item('p2', 'v2', 'chanA', '2026-01-01'),
            playlist_item('p1', 'v1', 'chanA', '2026-01-02'),
            playlist_item('p3', 'v3', 'chanA', '2026-01-03'),
        ]

        manager.sort_playlist(items)

        youtube.playlistItems().update.assert_not_called()

    def test_sort_key_needs_no_video_details(self):
        manager, youtube = manager_with_fake_api()

        manager.sort_playlist(
            [playlist_item('p1', 'v1', 'chanB', '2026-01-01'), playlist_item('p2', 'v2', 'chanA', '2026-01-01')]
        )

        youtube.videos().list.assert_not_called()


class SortCommandTest(unittest.TestCase):
    def test_deleted_video_in_playlist_does_not_crash_sort(self):
        manager, youtube = manager_with_fake_api()
        fake_pages(youtube.playlists(), [{'id': 'wl', 'snippet': {'title': 'Sort Watch Later'}}])
        fake_pages(
            youtube.playlistItems(),
            [
                playlist_item('p1', 'v2', 'chanA', '2026-01-02'),
                playlist_item('p2', 'deleted'),
                playlist_item('p3', 'v1', 'chanA', '2026-01-01'),
            ],
        )
        # videos.list has nothing for the deleted video.
        youtube.videos().list.return_value.execute.return_value = {
            'items': [
                {
                    'id': 'v1',
                    'snippet': {'channelId': 'chanA', 'publishedAt': '2026-01-01'},
                    'contentDetails': {'duration': 'PT1M'},
                },
                {
                    'id': 'v2',
                    'snippet': {'channelId': 'chanA', 'publishedAt': '2026-01-02'},
                    'contentDetails': {'duration': 'PT1M'},
                },
            ]
        }

        manager.sort()

        self.assertEqual(apply_moves(['p1', 'p2', 'p3'], moves_sent(youtube)), ['p2', 'p3', 'p1'])
        youtube.playlistItems().list.assert_called_once_with(
            part='snippet,contentDetails', playlistId='wl', maxResults=50
        )


class UpdateTestCase(unittest.TestCase):
    """Base for update() tests: config I/O is patched, API-facing methods are fakes."""

    dry_run = False

    def setUp(self):
        self.config = {'auto_add': [{'id': 'c1', 'name': 'Channel'}]}
        self.state = {'last_updated': '2026-01-01T00:00:00+00:00'}
        self.write_config = mock.Mock()
        self.write_state = mock.Mock()
        for name, value in [
            ('read_config', mock.Mock(return_value=self.config)),
            ('write_config', self.write_config),
            ('read_state', mock.Mock(return_value=self.state)),
            ('write_state', self.write_state),
        ]:
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
        self.write_state.assert_not_called()


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
        self.write_state.assert_not_called()


class UpdateWatermarkTest(UpdateTestCase):
    def test_watermark_is_run_start_minus_delay(self):
        run_start = arrow.get('2026-02-01T10:00:00+00:00')

        with mock.patch.object(arrow, 'now', return_value=run_start):
            self.manager.update(None)

        held_back = run_start.shift(hours=-3)
        self.assertEqual(self.fetch_args[1], held_back)
        self.assertEqual(self.state['last_updated'], held_back.format())

    def test_run_within_delay_fetches_nothing_and_keeps_watermark(self):
        self.state['last_updated'] = '2026-02-01T09:00:00+00:00'

        with mock.patch.object(arrow, 'now', return_value=arrow.get('2026-02-01T10:00:00+00:00')):
            self.manager.update(None)

        self.assertIsNone(self.fetch_args)
        self.write_config.assert_not_called()
        self.write_state.assert_not_called()
        self.assertEqual(self.state['last_updated'], '2026-02-01T09:00:00+00:00')

    def test_explicit_until_is_the_watermark_without_delay(self):
        until = arrow.get('2026-01-15T00:00:00+00:00')

        self.manager.update(None, until)

        self.assertEqual(self.fetch_args[1], until)
        self.assertEqual(self.state['last_updated'], until.format())

    def test_auto_batch_caps_inserts_and_watermark(self):
        base = arrow.get('2026-01-02T00:00:00+00:00')
        self.fetched = [
            {'id': f'v{i}', 'title': f'v{i}', 'published_at': base.shift(minutes=i).isoformat()}
            for i in range(playlist_updates.MAX_INSERTS_PER_RUN + 5)
        ]

        self.manager.update(None, auto_batch=True)

        inserted = self.manager.insert_videos_watch_later.call_args.args[0]
        self.assertEqual(len(inserted), playlist_updates.MAX_INSERTS_PER_RUN)
        cutoff = arrow.get(self.fetched[playlist_updates.MAX_INSERTS_PER_RUN]['published_at'])
        self.assertEqual(self.state['last_updated'], cutoff.format())


class PrintDurationTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(playlist_updates, 'print')
        self.print = patcher.start()
        self.addCleanup(patcher.stop)

    def test_total_over_a_day_keeps_the_days(self):
        YoutubeManager.print_duration({'v1': timedelta(hours=20), 'v2': timedelta(hours=10, minutes=5, seconds=59)})

        self.print.assert_called_with('Total duration of playlist is a day 6 hours and 5 minutes')

    def test_no_videos_prints_zero(self):
        YoutubeManager.print_duration({})

        self.print.assert_called_with('Total duration of playlist is 0 days 0 hours and 0 minutes')


class ConfigTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        self.config_file = root / 'config' / 'config.yaml'
        self.state_file = root / 'state' / 'state.yaml'
        self.legacy_file = root / 'cache' / 'config.yaml'
        for name, value in [
            ('CONFIG_FILE', self.config_file),
            ('STATE_FILE', self.state_file),
            ('LEGACY_CONFIG_FILE', self.legacy_file),
        ]:
            patcher = mock.patch.object(playlist_updates, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for cached in (playlist_updates.read_config, playlist_updates.read_state):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        print_patcher = mock.patch.object(playlist_updates, 'print')
        self.print = print_patcher.start()
        self.addCleanup(print_patcher.stop)

    def _write_legacy(self, data):
        self.legacy_file.parent.mkdir(parents=True)
        self.legacy_file.write_text(yaml.safe_dump(data))

    def _printed(self):
        return ' '.join(str(call.args[0]) for call in self.print.call_args_list)

    def test_interrupted_write_keeps_previous_config(self):
        playlist_updates.write_config({'auto_add': [{'id': 'c1', 'name': 'Channel'}]})

        def interrupted_dump(data, stream, **kwargs):
            stream.write('---\nauto_')
            raise KeyboardInterrupt

        with mock.patch.object(yaml, 'safe_dump', side_effect=interrupted_dump):
            with self.assertRaises(KeyboardInterrupt):
                playlist_updates.write_config({'auto_add': []})

        self.assertEqual(yaml.safe_load(self.config_file.read_text()), {'auto_add': [{'id': 'c1', 'name': 'Channel'}]})
        self.assertEqual(list(self.config_file.parent.iterdir()), [self.config_file])  # temp file cleaned up

    def test_new_config_is_owner_only(self):
        playlist_updates.write_config({'auto_add': []})

        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)

    def test_rewrite_keeps_existing_permissions(self):
        playlist_updates.write_config({'auto_add': []})
        self.config_file.chmod(0o640)

        playlist_updates.write_config({'auto_add': [{'id': 'c1', 'name': 'Channel'}]})

        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o640)

    def test_write_through_symlinked_config(self):
        real_file = self.config_file.parent / 'real-config.yaml'
        self.config_file.parent.mkdir(parents=True)
        real_file.write_text(yaml.safe_dump({'auto_add': []}))
        self.config_file.symlink_to(real_file)

        playlist_updates.write_config({'auto_add': [{'id': 'c1', 'name': 'Channel'}]})

        self.assertTrue(self.config_file.is_symlink())
        self.assertEqual(self.config_file.resolve(), real_file)
        self.assertEqual(yaml.safe_load(real_file.read_text()), {'auto_add': [{'id': 'c1', 'name': 'Channel'}]})

    LEGACY = {'auto_add': [{'id': 'c1', 'name': 'Channel'}], 'last_updated': '2026-01-01T00:00:00+00:00'}

    def test_help_does_not_migrate_legacy_config(self):
        self._write_legacy(self.LEGACY)

        with mock.patch('sys.argv', ['playlist_updates.py', 'update', '--help']):
            playlist_updates.main(ctx=mock.Mock(resilient_parsing=False), dry_run=False)

        self.assertFalse(self.config_file.exists())
        self.assertFalse(self.state_file.exists())

    def test_fresh_install_reads_empty_without_creating_files(self):
        playlist_updates.migrate_legacy_config(dry_run=False)

        self.assertEqual(playlist_updates.read_config(), {})
        self.assertEqual(playlist_updates.read_state(), {})
        self.assertFalse(self.config_file.exists())
        self.assertFalse(self.state_file.exists())
        self.print.assert_not_called()

    def test_migration_splits_legacy_keeps_it_and_warns(self):
        self._write_legacy(self.LEGACY)

        playlist_updates.migrate_legacy_config(dry_run=False)

        self.assertEqual(playlist_updates.read_config(), {'auto_add': [{'id': 'c1', 'name': 'Channel'}]})
        self.assertEqual(playlist_updates.read_state(), {'last_updated': '2026-01-01T00:00:00+00:00'})
        self.assertTrue(self.legacy_file.exists())
        self.assertIn('delete it', self._printed())

    def test_dry_run_reads_legacy_without_writing(self):
        self._write_legacy(self.LEGACY)

        playlist_updates.migrate_legacy_config(dry_run=True)

        self.assertEqual(playlist_updates.read_config(), {'auto_add': [{'id': 'c1', 'name': 'Channel'}]})
        self.assertEqual(playlist_updates.read_state(), {'last_updated': '2026-01-01T00:00:00+00:00'})
        self.assertFalse(self.config_file.exists())
        self.assertFalse(self.state_file.exists())

    def test_after_migration_new_files_win_and_warning_repeats(self):
        self._write_legacy({'auto_add': [{'id': 'old', 'name': 'Old'}]})
        playlist_updates.write_config({'auto_add': [{'id': 'new', 'name': 'New'}]})

        playlist_updates.migrate_legacy_config(dry_run=False)

        self.assertEqual(playlist_updates.read_config(), {'auto_add': [{'id': 'new', 'name': 'New'}]})
        self.assertTrue(self.legacy_file.exists())
        self.assertIn('delete it', self._printed())


class AuthPathsTest(unittest.TestCase):
    def test_client_secrets_resolve_next_to_script_not_cwd(self):
        script_dir = Path(playlist_updates.__file__).resolve().parent

        self.assertEqual(playlist_updates.CLIENT_SECRETS_FILE, script_dir / 'client_secrets.json')


class GetCredsTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        self.token_file = root / 'config' / 'token.json'
        self.legacy_token_file = root / 'app' / 'playlist_updates.py-oauth2.json'
        self.client_secrets_file = root / 'app' / 'client_secrets.json'
        self.client_secrets_file.parent.mkdir(parents=True)
        self.client_secrets_file.write_text('{}')
        for name, value in [
            ('TOKEN_FILE', self.token_file),
            ('LEGACY_TOKEN_FILE', self.legacy_token_file),
            ('CLIENT_SECRETS_FILE', self.client_secrets_file),
        ]:
            patcher = mock.patch.object(playlist_updates, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        flow_patcher = mock.patch.object(playlist_updates.InstalledAppFlow, 'from_client_secrets_file')
        self.flow = flow_patcher.start().return_value
        self.addCleanup(flow_patcher.stop)
        self.flow.run_local_server.return_value = self._credentials(valid=True)

    @staticmethod
    def _credentials(valid, refresh_token='refresh'):
        return mock.Mock(valid=valid, expired=not valid, refresh_token=refresh_token, **{'to_json.return_value': '{}'})

    def _cached(self, credentials):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text('{}')
        patcher = mock.patch.object(playlist_updates.Credentials, 'from_authorized_user_file', return_value=credentials)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_cached_token_skips_consent(self):
        cached = self._credentials(valid=True)
        self._cached(cached)

        self.assertIs(YoutubeManager.get_creds(), cached)
        self.flow.run_local_server.assert_not_called()

    def test_expired_token_is_refreshed_and_saved(self):
        cached = self._credentials(valid=False)
        self._cached(cached)

        self.assertIs(YoutubeManager.get_creds(), cached)
        cached.refresh.assert_called_once()
        self.flow.run_local_server.assert_not_called()

    def test_revoked_refresh_token_falls_back_to_consent(self):
        cached = self._credentials(valid=False)
        cached.refresh.side_effect = playlist_updates.google.auth.exceptions.RefreshError('invalid_grant')
        self._cached(cached)

        YoutubeManager.get_creds()

        self.flow.run_local_server.assert_called_once_with(port=8080)

    def test_first_run_saves_owner_only_token_and_keeps_legacy_token(self):
        self.legacy_token_file.write_text('{}')

        with mock.patch.object(playlist_updates, 'print') as fake_print:
            YoutubeManager.get_creds()

        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertTrue(self.legacy_token_file.exists())
        self.assertIn(str(self.legacy_token_file), ' '.join(str(c.args[0]) for c in fake_print.call_args_list))

    def test_corrupt_token_file_falls_back_to_consent(self):
        self.token_file.parent.mkdir(parents=True)
        for contents in ['not json', '[]', '"x"']:
            with self.subTest(contents=contents):
                self.flow.run_local_server.reset_mock()
                self.token_file.write_text(contents)

                YoutubeManager.get_creds()

                self.flow.run_local_server.assert_called_once_with(port=8080)

    def test_refresh_forces_owner_only_even_if_token_file_was_more_permissive(self):
        cached = self._credentials(valid=False)
        self._cached(cached)
        self.token_file.chmod(0o644)

        YoutubeManager.get_creds()

        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)

    def test_missing_client_secrets_exits_with_instructions(self):
        self.client_secrets_file.unlink()

        with self.assertRaises(SystemExit) as context:
            YoutubeManager.get_creds()
        self.assertIn(str(self.client_secrets_file), str(context.exception.code))


class UpdateStaleAllowlistTest(UpdateTestCase):
    def test_warns_about_allowlisted_channels_no_longer_subscribed(self):
        self.config['auto_add'].append({'id': 'c2', 'name': 'Gone Channel'})

        with mock.patch.object(playlist_updates, 'print') as fake_print:
            self.manager.update(None)

        messages = ' '.join(str(call.args[0]) for call in fake_print.call_args_list)
        self.assertIn('Gone Channel', messages)


class ListSubscriptionsTest(unittest.TestCase):
    def setUp(self):
        config = {'auto_add': [{'id': 'c1', 'name': 'Kept Channel'}, {'id': 'c2', 'name': 'Gone Channel'}]}
        patcher = mock.patch.object(playlist_updates, 'read_config', return_value=config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager, _ = manager_with_fake_api()
        self.manager.get_subscribed_channels = mock.Mock(return_value=[{'id': 'c1', 'title': 'Kept Channel'}])

    def _render(self, **kwargs):
        buffer = io.StringIO()
        with mock.patch.object(playlist_updates, 'Console', return_value=Console(file=buffer, width=120)):
            self.manager.list_subscriptions(**kwargs)
        return buffer.getvalue()

    def test_check_marks_channels_no_longer_subscribed(self):
        output = self._render(check=True)

        rows = {line.split()[1]: line for line in output.splitlines() if 'Channel' in line and '│' in line}
        self.assertIn('yes', rows['Kept'])
        self.assertIn('no', rows['Gone'])

    def test_plain_list_stays_local(self):
        output = self._render()

        self.manager.get_subscribed_channels.assert_not_called()
        self.assertNotIn('Subscribed', output)


if __name__ == '__main__':
    unittest.main()
