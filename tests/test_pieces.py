"""Tests for piece-state reporting and rarity bucketing (the heatmap data)."""


import libtorrent as lt
import pytest

from terminal_torrenter.config import Config
from terminal_torrenter.core import (
    SessionManager,
    TorrentStore,
    downloading_pieces_from_queue,
    rarity_level,
)

PIECE_SIZE = 16 * 1024
NUM_PIECES = 8


@pytest.fixture
def torrent_path(tmp_path):
    data = tmp_path / "movie.bin"
    data.write_bytes(b"a" * (PIECE_SIZE * NUM_PIECES))
    storage = lt.file_storage()
    lt.add_files(storage, str(data))
    creator = lt.create_torrent(storage, PIECE_SIZE)
    lt.set_piece_hashes(creator, str(tmp_path))
    torrent_file = tmp_path / "movie.torrent"
    torrent_file.write_bytes(lt.bencode(creator.generate()))
    return str(torrent_file)


@pytest.fixture
def manager(tmp_path):
    mgr = SessionManager(
        Config(download_dir=str(tmp_path / "out"), listen_port=6905),
        store=TorrentStore(str(tmp_path / "state",
    )),
    )
    yield mgr
    mgr.session.pause()


@pytest.mark.parametrize(
    "availability, expected_level",
    [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 3),
        (5, 4),
        (50, 4),
        (-1, 0),  # defensive: negative treated as rarest
    ],
)
def test_rarity_level_buckets(availability, expected_level):
    assert rarity_level(availability) == expected_level


def test_piece_state_shapes_match_piece_count(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    have, downloading, availability = manager.piece_state(tid)

    assert len(have) == NUM_PIECES
    assert len(availability) == NUM_PIECES
    assert isinstance(downloading, set)
    # Freshly added with no peers: nothing downloaded or in flight.
    assert not any(have)
    assert downloading == set()


def test_downloading_pieces_parsed_from_dict_queue():
    # libtorrent returns the download queue as dicts keyed by "piece_index".
    queue = [
        {"piece_index": 967, "blocks_in_piece": 8, "blocks": []},
        {"piece_index": 12, "blocks_in_piece": 8, "blocks": []},
    ]
    assert downloading_pieces_from_queue(queue) == {967, 12}


def test_downloading_pieces_parsed_from_object_queue():
    class FakePartial:
        def __init__(self, piece_index):
            self.piece_index = piece_index

    assert downloading_pieces_from_queue([FakePartial(3), FakePartial(5)]) == {3, 5}


def test_downloading_pieces_empty_queue():
    assert downloading_pieces_from_queue([]) == set()


def test_piece_state_unknown_torrent_is_empty(manager):
    assert manager.piece_state("does-not-exist") == ([], set(), [])


def test_piece_length_reported(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    assert manager.piece_length(tid) == PIECE_SIZE
