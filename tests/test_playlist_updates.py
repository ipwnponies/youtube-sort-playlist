import io
import random
import stat
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import arrow
import pytest
import yaml
from rich.console import Console

import playlist_updates
from playlist_updates import YoutubeManager, plan_moves

CHANNEL = {'id': 'c1', 'name': 'Channel'}
LEGACY_CONFIG = {'auto_add': [CHANNEL], 'last_updated': '2026-01-01T00:00:00+00:00'}


def make_manager(dry_run=False):
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


def printed_text(fake_print):
    return ' '.join(str(call.args[0]) for call in fake_print.call_args_list)


@pytest.fixture
def api():
    manager, youtube = make_manager()
    return SimpleNamespace(manager=manager, youtube=youtube)


@pytest.fixture
def fake_print(monkeypatch):
    fake = mock.Mock()
    monkeypatch.setattr(playlist_updates, 'print', fake)
    return fake


def build_update_env(monkeypatch, dry_run):
    """Config I/O is patched, API-facing methods are fakes."""
    env = SimpleNamespace(
        config={'auto_add': [dict(CHANNEL)]},
        state={'last_updated': '2026-01-01T00:00:00+00:00'},
        write_config=mock.Mock(),
        write_state=mock.Mock(),
        fetched=[],
        fetch_args=None,
    )
    monkeypatch.setattr(playlist_updates, 'read_config', mock.Mock(return_value=env.config))
    monkeypatch.setattr(playlist_updates, 'write_config', env.write_config)
    monkeypatch.setattr(playlist_updates, 'read_state', mock.Mock(return_value=env.state))
    monkeypatch.setattr(playlist_updates, 'write_state', env.write_state)

    env.manager, env.youtube = make_manager(dry_run)
    fake_pages(env.youtube.playlists(), [{'id': 'wl', 'snippet': {'title': 'Sort Watch Later'}}])
    env.manager.get_subscribed_channels = mock.Mock(return_value=[{'id': 'c1', 'title': 'Channel'}])
    env.manager.insert_videos_watch_later = mock.Mock()

    async def fake_fetch(channels, uploaded_after, uploaded_until):
        env.fetch_args = (uploaded_after, uploaded_until)
        return list(env.fetched)

    env.manager.fetch_all_channels_videos = fake_fetch
    return env


@pytest.fixture
def update_env(monkeypatch):
    return build_update_env(monkeypatch, dry_run=False)


@pytest.fixture
def dry_update_env(monkeypatch):
    return build_update_env(monkeypatch, dry_run=True)


@pytest.fixture
def config_env(tmp_path, monkeypatch, fake_print):
    env = SimpleNamespace(
        config_file=tmp_path / 'config' / 'config.yaml',
        state_file=tmp_path / 'state' / 'state.yaml',
        legacy_file=tmp_path / 'cache' / 'config.yaml',
        print=fake_print,
    )
    monkeypatch.setattr(playlist_updates, 'CONFIG_FILE', env.config_file)
    monkeypatch.setattr(playlist_updates, 'STATE_FILE', env.state_file)
    monkeypatch.setattr(playlist_updates, 'LEGACY_CONFIG_FILE', env.legacy_file)
    for cached in (playlist_updates.read_config, playlist_updates.read_state):
        cached.cache_clear()
    yield env
    for cached in (playlist_updates.read_config, playlist_updates.read_state):
        cached.cache_clear()


@pytest.fixture
def legacy_config(config_env):
    config_env.legacy_file.parent.mkdir(parents=True)
    config_env.legacy_file.write_text(yaml.safe_dump(LEGACY_CONFIG))
    return config_env


def test_finds_playlist_beyond_first_page(api):
    fake_pages(
        api.youtube.playlists(),
        [{'id': 'other', 'snippet': {'title': 'Other'}}],
        [{'id': 'target', 'snippet': {'title': 'Sort Watch Later'}}],
    )

    assert api.manager.get_watchlater_playlist() == 'target'
    api.youtube.playlists().list.assert_called_once_with(part='snippet', mine=True, maxResults=50)


def test_missing_playlist_exits_with_message(api):
    fake_pages(api.youtube.playlists(), [{'id': 'other', 'snippet': {'title': 'Other'}}])

    with pytest.raises(SystemExit) as excinfo:
        api.manager.get_watchlater_playlist()
    assert 'Sort Watch Later' in str(excinfo.value.code)


def test_video_durations_are_queried_in_batches_of_50(api):
    video_ids = [f'v{i}' for i in range(51)]
    api.youtube.videos().list.return_value.execute.side_effect = lambda: {
        'items': [{'id': 'v0', 'contentDetails': {'duration': 'PT1M'}}]
    }

    durations = api.manager.get_video_durations([playlist_item(f'p{i}', v) for i, v in enumerate(video_ids)])

    queried = [call.kwargs['id'].split(',') for call in api.youtube.videos().list.call_args_list]
    assert queried == [video_ids[:50], video_ids[50:]]
    assert durations == {'v0': timedelta(minutes=1)}


@pytest.mark.parametrize(
    ('current', 'target', 'expected'),
    [
        pytest.param(['a', 'b', 'c'], ['a', 'b', 'c'], [], id='already-sorted'),
        pytest.param(['z', 'a', 'b', 'c'], ['a', 'b', 'c', 'z'], [('z', 3)], id='single-misplaced-item'),
    ],
)
def test_plan_moves(current, target, expected):
    assert plan_moves(current, target) == expected


@pytest.mark.parametrize('size', range(40))
def test_random_permutations_reach_target_in_fewest_moves(size):
    target = [f'item{i}' for i in range(size)]
    current = target[:]
    random.Random(size).shuffle(current)

    moves = plan_moves(current, target)

    assert apply_moves(current, moves) == target
    assert len(moves) == size - lis_length([target.index(i) for i in current])


def test_only_out_of_place_items_are_updated(api):
    items = [
        playlist_item('p1', 'v1', 'chanA', '2026-01-02'),
        playlist_item('p2', 'v2', 'chanA', '2026-01-03'),
        playlist_item('p3', 'v3', 'chanA', '2026-01-01'),
    ]

    api.manager.sort_playlist(items)

    assert moves_sent(api.youtube) == [('p3', 0)]


def test_same_video_twice_is_handled_per_playlist_item(api):
    items = [
        playlist_item('p1', 'v2', 'chanA', '2026-01-02'),
        playlist_item('p2', 'v1', 'chanA', '2026-01-01'),
        playlist_item('p3', 'v2', 'chanA', '2026-01-02'),
    ]

    api.manager.sort_playlist(items)

    moves = moves_sent(api.youtube)
    assert len(moves) == 1
    assert apply_moves(['p1', 'p2', 'p3'], moves) == ['p2', 'p1', 'p3']


def test_sort_dry_run_does_not_update():
    manager, youtube = make_manager(dry_run=True)
    items = [playlist_item('p1', 'v1', 'chanB', '2026-01-01'), playlist_item('p2', 'v2', 'chanA', '2026-01-01')]

    manager.sort_playlist(items)

    youtube.playlistItems().update.assert_not_called()


def test_unavailable_entries_move_to_front_before_anything_else(api):
    items = [
        playlist_item('p1', 'v1', 'chanA', '2026-01-02'),
        playlist_item('p2', 'v2', 'chanA', '2026-01-01'),
        playlist_item('p3', 'v3', 'chanA', '2026-01-03'),
        playlist_item('p4', 'deleted'),
    ]

    api.manager.sort_playlist(items)

    moves = moves_sent(api.youtube)
    assert moves == [('p4', 0), ('p1', 2)]
    assert apply_moves(['p1', 'p2', 'p3', 'p4'], moves) == ['p4', 'p2', 'p1', 'p3']


def test_second_sort_makes_no_moves(api):
    items = [
        playlist_item('p4', 'deleted'),
        playlist_item('p2', 'v2', 'chanA', '2026-01-01'),
        playlist_item('p1', 'v1', 'chanA', '2026-01-02'),
        playlist_item('p3', 'v3', 'chanA', '2026-01-03'),
    ]

    api.manager.sort_playlist(items)

    api.youtube.playlistItems().update.assert_not_called()


def test_sort_key_needs_no_video_details(api):
    api.manager.sort_playlist(
        [playlist_item('p1', 'v1', 'chanB', '2026-01-01'), playlist_item('p2', 'v2', 'chanA', '2026-01-01')]
    )

    api.youtube.videos().list.assert_not_called()


def test_deleted_video_in_playlist_does_not_crash_sort(api):
    youtube = api.youtube
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

    api.manager.sort()

    assert apply_moves(['p1', 'p2', 'p3'], moves_sent(youtube)) == ['p2', 'p3', 'p1']
    youtube.playlistItems().list.assert_called_once_with(part='snippet,contentDetails', playlistId='wl', maxResults=50)


def test_update_missing_playlist_exits_before_fetching_or_writing(update_env):
    fake_pages(update_env.youtube.playlists(), [])

    with pytest.raises(SystemExit):
        update_env.manager.update(None)

    update_env.manager.get_subscribed_channels.assert_not_called()
    update_env.write_config.assert_not_called()
    update_env.write_state.assert_not_called()


def test_dry_run_also_exits_when_playlist_missing(dry_update_env):
    fake_pages(dry_update_env.youtube.playlists(), [])

    with pytest.raises(SystemExit):
        dry_update_env.manager.update(None)

    dry_update_env.manager.get_subscribed_channels.assert_not_called()


def test_dry_run_with_playlist_writes_nothing(dry_update_env):
    dry_update_env.manager.update(None)

    dry_update_env.write_config.assert_not_called()
    dry_update_env.write_state.assert_not_called()


def test_watermark_is_run_start_minus_delay(update_env):
    run_start = arrow.get('2026-02-01T10:00:00+00:00')

    with mock.patch.object(arrow, 'now', return_value=run_start):
        update_env.manager.update(None)

    held_back = run_start.shift(hours=-3)
    assert update_env.fetch_args[1] == held_back
    assert update_env.state['last_updated'] == held_back.format()


def test_run_within_delay_fetches_nothing_and_keeps_watermark(update_env):
    update_env.state['last_updated'] = '2026-02-01T09:00:00+00:00'

    with mock.patch.object(arrow, 'now', return_value=arrow.get('2026-02-01T10:00:00+00:00')):
        update_env.manager.update(None)

    assert update_env.fetch_args is None
    update_env.write_config.assert_not_called()
    update_env.write_state.assert_not_called()
    assert update_env.state['last_updated'] == '2026-02-01T09:00:00+00:00'


def test_explicit_until_is_the_watermark_without_delay(update_env):
    until = arrow.get('2026-01-15T00:00:00+00:00')

    update_env.manager.update(None, until)

    assert update_env.fetch_args[1] == until
    assert update_env.state['last_updated'] == until.format()


def test_auto_batch_caps_inserts_and_watermark(update_env):
    base = arrow.get('2026-01-02T00:00:00+00:00')
    update_env.fetched = [
        {'id': f'v{i}', 'title': f'v{i}', 'published_at': base.shift(minutes=i).isoformat()}
        for i in range(playlist_updates.MAX_INSERTS_PER_RUN + 5)
    ]

    update_env.manager.update(None, auto_batch=True)

    inserted = update_env.manager.insert_videos_watch_later.call_args.args[0]
    assert len(inserted) == playlist_updates.MAX_INSERTS_PER_RUN
    cutoff = arrow.get(update_env.fetched[playlist_updates.MAX_INSERTS_PER_RUN]['published_at'])
    assert update_env.state['last_updated'] == cutoff.format()


def test_warns_about_allowlisted_channels_no_longer_subscribed(update_env, fake_print):
    update_env.config['auto_add'].append({'id': 'c2', 'name': 'Gone Channel'})

    update_env.manager.update(None)

    assert 'Gone Channel' in printed_text(fake_print)


@pytest.mark.parametrize(
    ('durations', 'expected'),
    [
        pytest.param(
            {'v1': timedelta(hours=20), 'v2': timedelta(hours=10, minutes=5, seconds=59)},
            'Total duration of playlist is a day 6 hours and 5 minutes',
            id='over-a-day-keeps-the-days',
        ),
        pytest.param({}, 'Total duration of playlist is 0 days 0 hours and 0 minutes', id='no-videos-prints-zero'),
    ],
)
def test_print_duration(fake_print, durations, expected):
    YoutubeManager.print_duration(durations)

    fake_print.assert_called_with(expected)


def test_interrupted_write_keeps_previous_config(config_env):
    playlist_updates.write_config({'auto_add': [CHANNEL]})

    def interrupted_dump(data, stream, **kwargs):
        stream.write('---\nauto_')
        raise KeyboardInterrupt

    with mock.patch.object(yaml, 'safe_dump', side_effect=interrupted_dump):
        with pytest.raises(KeyboardInterrupt):
            playlist_updates.write_config({'auto_add': []})

    assert yaml.safe_load(config_env.config_file.read_text()) == {'auto_add': [CHANNEL]}
    assert list(config_env.config_file.parent.iterdir()) == [config_env.config_file]  # temp file cleaned up


def test_new_config_is_owner_only(config_env):
    playlist_updates.write_config({'auto_add': []})

    assert stat.S_IMODE(config_env.config_file.stat().st_mode) == 0o600


def test_rewrite_keeps_existing_permissions(config_env):
    playlist_updates.write_config({'auto_add': []})
    config_env.config_file.chmod(0o640)

    playlist_updates.write_config({'auto_add': [CHANNEL]})

    assert stat.S_IMODE(config_env.config_file.stat().st_mode) == 0o640


def test_write_through_symlinked_config(config_env):
    real_file = config_env.config_file.parent / 'real-config.yaml'
    config_env.config_file.parent.mkdir(parents=True)
    real_file.write_text(yaml.safe_dump({'auto_add': []}))
    config_env.config_file.symlink_to(real_file)

    playlist_updates.write_config({'auto_add': [CHANNEL]})

    assert config_env.config_file.is_symlink()
    assert config_env.config_file.resolve() == real_file
    assert yaml.safe_load(real_file.read_text()) == {'auto_add': [CHANNEL]}


def test_help_does_not_migrate_legacy_config(legacy_config):
    with mock.patch('sys.argv', ['playlist_updates.py', 'update', '--help']):
        playlist_updates.main(ctx=mock.Mock(resilient_parsing=False), dry_run=False)

    assert not legacy_config.config_file.exists()
    assert not legacy_config.state_file.exists()


def test_fresh_install_reads_empty_without_creating_files(config_env):
    playlist_updates.migrate_legacy_config(dry_run=False)

    assert playlist_updates.read_config() == {}
    assert playlist_updates.read_state() == {}
    assert not config_env.config_file.exists()
    assert not config_env.state_file.exists()
    config_env.print.assert_not_called()


def test_migration_splits_legacy_keeps_it_and_warns(legacy_config):
    playlist_updates.migrate_legacy_config(dry_run=False)

    assert playlist_updates.read_config() == {'auto_add': [CHANNEL]}
    assert playlist_updates.read_state() == {'last_updated': '2026-01-01T00:00:00+00:00'}
    assert legacy_config.legacy_file.exists()
    assert 'delete it' in printed_text(legacy_config.print)


def test_dry_run_reads_legacy_without_writing(legacy_config):
    playlist_updates.migrate_legacy_config(dry_run=True)

    assert playlist_updates.read_config() == {'auto_add': [CHANNEL]}
    assert playlist_updates.read_state() == {'last_updated': '2026-01-01T00:00:00+00:00'}
    assert not legacy_config.config_file.exists()
    assert not legacy_config.state_file.exists()


def test_after_migration_new_files_win_and_warning_repeats(legacy_config):
    playlist_updates.write_config({'auto_add': [{'id': 'new', 'name': 'New'}]})

    playlist_updates.migrate_legacy_config(dry_run=False)

    assert playlist_updates.read_config() == {'auto_add': [{'id': 'new', 'name': 'New'}]}
    assert legacy_config.legacy_file.exists()
    assert 'delete it' in printed_text(legacy_config.print)


def test_client_secrets_resolve_next_to_script_not_cwd():
    script_dir = Path(playlist_updates.__file__).resolve().parent

    assert playlist_updates.CLIENT_SECRETS_FILE == script_dir / 'client_secrets.json'


def credentials(valid, refresh_token='refresh'):
    return mock.Mock(valid=valid, expired=not valid, refresh_token=refresh_token, **{'to_json.return_value': '{}'})


@pytest.fixture
def creds_env(tmp_path, monkeypatch):
    env = SimpleNamespace(
        token_file=tmp_path / 'config' / 'token.json',
        legacy_token_file=tmp_path / 'app' / 'playlist_updates.py-oauth2.json',
        client_secrets_file=tmp_path / 'app' / 'client_secrets.json',
    )
    env.client_secrets_file.parent.mkdir(parents=True)
    env.client_secrets_file.write_text('{}')
    monkeypatch.setattr(playlist_updates, 'TOKEN_FILE', env.token_file)
    monkeypatch.setattr(playlist_updates, 'LEGACY_TOKEN_FILE', env.legacy_token_file)
    monkeypatch.setattr(playlist_updates, 'CLIENT_SECRETS_FILE', env.client_secrets_file)

    from_secrets = mock.Mock()
    monkeypatch.setattr(playlist_updates.InstalledAppFlow, 'from_client_secrets_file', from_secrets)
    env.flow = from_secrets.return_value
    env.flow.run_local_server.return_value = credentials(valid=True)

    def cache(cached):
        env.token_file.parent.mkdir(parents=True, exist_ok=True)
        env.token_file.write_text('{}')
        monkeypatch.setattr(playlist_updates.Credentials, 'from_authorized_user_file', mock.Mock(return_value=cached))

    env.cache = cache
    return env


def test_valid_cached_token_skips_consent(creds_env):
    cached = credentials(valid=True)
    creds_env.cache(cached)

    assert YoutubeManager.get_creds() is cached
    creds_env.flow.run_local_server.assert_not_called()


def test_expired_token_is_refreshed_and_saved(creds_env):
    cached = credentials(valid=False)
    creds_env.cache(cached)

    assert YoutubeManager.get_creds() is cached
    cached.refresh.assert_called_once()
    creds_env.flow.run_local_server.assert_not_called()


def test_revoked_refresh_token_falls_back_to_consent(creds_env):
    cached = credentials(valid=False)
    cached.refresh.side_effect = playlist_updates.google.auth.exceptions.RefreshError('invalid_grant')
    creds_env.cache(cached)

    YoutubeManager.get_creds()

    creds_env.flow.run_local_server.assert_called_once_with(port=8080)


def test_first_run_saves_owner_only_token_and_keeps_legacy_token(creds_env, fake_print):
    creds_env.legacy_token_file.write_text('{}')

    YoutubeManager.get_creds()

    assert stat.S_IMODE(creds_env.token_file.stat().st_mode) == 0o600
    assert creds_env.legacy_token_file.exists()
    assert str(creds_env.legacy_token_file) in printed_text(fake_print)


@pytest.mark.parametrize('contents', ['not json', '[]', '"x"'])
def test_corrupt_token_file_falls_back_to_consent(creds_env, contents):
    creds_env.token_file.parent.mkdir(parents=True)
    creds_env.token_file.write_text(contents)

    YoutubeManager.get_creds()

    creds_env.flow.run_local_server.assert_called_once_with(port=8080)


def test_refresh_forces_owner_only_even_if_token_file_was_more_permissive(creds_env):
    creds_env.cache(credentials(valid=False))
    creds_env.token_file.chmod(0o644)

    YoutubeManager.get_creds()

    assert stat.S_IMODE(creds_env.token_file.stat().st_mode) == 0o600


def test_missing_client_secrets_exits_with_instructions(creds_env):
    creds_env.client_secrets_file.unlink()

    with pytest.raises(SystemExit) as excinfo:
        YoutubeManager.get_creds()
    assert str(creds_env.client_secrets_file) in str(excinfo.value.code)


@pytest.fixture
def subscriptions_env(monkeypatch):
    config = {'auto_add': [{'id': 'c1', 'name': 'Kept Channel'}, {'id': 'c2', 'name': 'Gone Channel'}]}
    monkeypatch.setattr(playlist_updates, 'read_config', mock.Mock(return_value=config))
    manager, _ = make_manager()
    manager.get_subscribed_channels = mock.Mock(return_value=[{'id': 'c1', 'title': 'Kept Channel'}])

    def render(**kwargs):
        buffer = io.StringIO()
        with mock.patch.object(playlist_updates, 'Console', return_value=Console(file=buffer, width=120)):
            manager.list_subscriptions(**kwargs)
        return buffer.getvalue()

    return SimpleNamespace(manager=manager, render=render)


def test_check_marks_channels_no_longer_subscribed(subscriptions_env):
    output = subscriptions_env.render(check=True)

    rows = {line.split()[1]: line for line in output.splitlines() if 'Channel' in line and '│' in line}
    assert 'yes' in rows['Kept']
    assert 'no' in rows['Gone']


def test_plain_list_stays_local(subscriptions_env):
    output = subscriptions_env.render()

    subscriptions_env.manager.get_subscribed_channels.assert_not_called()
    assert 'Subscribed' not in output
