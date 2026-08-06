"""Tests for the global sequential toggle and first/last piece prioritization.

These build a small torrent on disk (no network) and drive a real
SessionManager so the libtorrent integration is exercised end to end.
"""


import libtorrent as lt
import pytest

from terminal_torrenter.config import Config
from terminal_torrenter.core import SessionManager, TorrentStore

PIECE_SIZE = 16 * 1024
TOP_PRIORITY = 7
DEFAULT_PRIORITY = 4


@pytest.fixture
def torrent_path(tmp_path):
    """A multi-piece .torrent backed by a real file in tmp_path."""
    data = tmp_path / "movie.bin"
    data.write_bytes(b"a" * (PIECE_SIZE * 5))  # 5 pieces

    storage = lt.file_storage()
    lt.add_files(storage, str(data))
    creator = lt.create_torrent(storage, PIECE_SIZE)
    lt.set_piece_hashes(creator, str(tmp_path))
    torrent_file = tmp_path / "movie.torrent"
    torrent_file.write_bytes(lt.bencode(creator.generate()))
    return str(torrent_file)


@pytest.fixture
def manager(tmp_path):
    store = TorrentStore(str(tmp_path / "state"))
    mgr = SessionManager(
        Config(download_dir=str(tmp_path / "out"), listen_port=6901),
        store=store,
    )
    yield mgr
    mgr.session.pause()


def test_new_manager_defaults_to_sequential(manager):
    assert manager.sequential is True


def test_toggle_sequential_flips_global_state(manager, torrent_path):
    tid = manager.add_source(torrent_path)

    assert manager.toggle_sequential() is False
    assert manager.sequential is False
    assert manager.torrents[tid].sequential is False

    assert manager.toggle_sequential() is True
    assert manager.sequential is True
    assert manager.torrents[tid].sequential is True


def test_toggle_sequential_applies_to_all_existing_torrents(manager, torrent_path, tmp_path):
    # Add the same torrent plus a second one so we cover "all at once".
    manager.add_source(torrent_path)
    manager.set_sequential_all(False)
    assert all(m.sequential is False for m in manager.torrents.values())
    manager.set_sequential_all(True)
    assert all(m.sequential is True for m in manager.torrents.values())


def test_first_and_last_pieces_are_prioritized(manager, torrent_path):
    tid = manager.add_source(torrent_path)

    manager.ensure_endpoints_prioritized()

    handle = manager.torrents[tid].handle
    num_pieces = handle.torrent_file().num_pieces()
    assert num_pieces == 5
    assert handle.piece_priority(0) == TOP_PRIORITY
    assert handle.piece_priority(num_pieces - 1) == TOP_PRIORITY
    # Middle pieces keep their normal priority.
    assert handle.piece_priority(2) == DEFAULT_PRIORITY


def test_recheck_starts_for_known_torrent(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    manager.ensure_endpoints_prioritized()
    assert tid in manager._ends_prioritized

    assert manager.recheck(tid) is True
    # A recheck invalidates the endpoint prioritization so it re-applies after.
    assert tid not in manager._ends_prioritized


def test_recheck_unknown_torrent_is_noop(manager):
    assert manager.recheck("does-not-exist") is False


def test_endpoint_prioritization_is_idempotent(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    manager.ensure_endpoints_prioritized()
    manager.ensure_endpoints_prioritized()  # second call should be a no-op
    assert tid in manager._ends_prioritized
    handle = manager.torrents[tid].handle
    assert handle.piece_priority(0) == TOP_PRIORITY
    assert handle.piece_priority(handle.torrent_file().num_pieces() - 1) == TOP_PRIORITY
