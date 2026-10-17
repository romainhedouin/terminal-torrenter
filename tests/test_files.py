"""Picking which files/folders of a torrent to download.

The tree helpers are exercised on plain data; the priority plumbing runs against
a real multi-file torrent built on disk (no network).
"""

import libtorrent as lt
import pytest

from terminal_torrenter.config import Config
from terminal_torrenter.core import (
    FILE_PRIORITY_NORMAL,
    FILE_PRIORITY_SKIP,
    SessionManager,
    TorrentFile,
    TorrentStore,
    build_file_tree,
    flatten_tree,
)

PIECE_SIZE = 16 * 1024
FILE_LAYOUT = (
    ("show/season1/e01.bin", 2),
    ("show/season1/e02.bin", 2),
    ("show/extras/behind.bin", 1),
    ("show/readme.bin", 1),
)


@pytest.fixture
def torrent_path(tmp_path):
    """A multi-file, multi-folder .torrent backed by real files."""
    for relative, pieces in FILE_LAYOUT:
        path = tmp_path / "content" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (PIECE_SIZE * pieces))

    storage = lt.file_storage()
    lt.add_files(storage, str(tmp_path / "content"))
    creator = lt.create_torrent(storage, PIECE_SIZE)
    lt.set_piece_hashes(creator, str(tmp_path))
    torrent_file = tmp_path / "multi.torrent"
    torrent_file.write_bytes(lt.bencode(creator.generate()))
    return str(torrent_file)


@pytest.fixture
def manager(tmp_path):
    store = TorrentStore(str(tmp_path / "state"))
    mgr = SessionManager(
        Config(download_dir=str(tmp_path / "out"), listen_port=6903),
        store=store,
    )
    yield mgr
    mgr.session.pause()


def index_of(files, suffix):
    return next(f.index for f in files if f.path.endswith(suffix))


def find_dir(nodes, name):
    """Depth-first search for a folder node; the torrent has a root dir on top."""

    def search(candidates):
        for node in candidates:
            if not node.is_dir:
                continue
            if node.name == name:
                return node
            found = search(node.children)
            if found is not None:
                return found
        return None

    found = search(nodes)
    assert found is not None, f"no folder named {name}"
    return found


# -- tree helpers ----------------------------------------------------------- #


def sample_files():
    return [
        TorrentFile(0, "show/season1/e01.mkv", 100, downloaded=50),
        TorrentFile(1, "show/season1/e02.mkv", 100),
        TorrentFile(2, "show/readme.txt", 10, downloaded=10),
        TorrentFile(3, "top.nfo", 5, downloaded=5),
    ]


def test_tree_groups_files_by_folder():
    roots = build_file_tree(sample_files())

    assert [(node.name, node.is_dir) for node in roots] == [("show", True), ("top.nfo", False)]
    show = roots[0]
    # Folders sort before files inside a folder.
    assert [node.name for node in show.children] == ["season1", "readme.txt"]
    assert [node.name for node in show.children[0].children] == ["e01.mkv", "e02.mkv"]


def test_folders_aggregate_size_progress_and_indices():
    show = build_file_tree(sample_files())[0]

    assert show.size == 210
    assert show.downloaded == 60
    assert sorted(show.indices) == [0, 1, 2]
    assert sorted(show.children[0].indices) == [0, 1]
    assert show.children[0].progress == pytest.approx(0.25)


def test_leaves_carry_their_file_index_and_depth():
    leaf = build_file_tree(sample_files())[0].children[0].children[0]

    assert leaf.file_index == 0
    assert leaf.depth == 2
    assert leaf.is_dir is False


def test_flatten_hides_children_of_collapsed_folders():
    roots = build_file_tree(sample_files())

    expanded = [node.path for node in flatten_tree(roots, set())]
    assert expanded == [
        "show",
        "show/season1",
        "show/season1/e01.mkv",
        "show/season1/e02.mkv",
        "show/readme.txt",
        "top.nfo",
    ]

    collapsed = [node.path for node in flatten_tree(roots, {"show/season1"})]
    assert collapsed == ["show", "show/season1", "show/readme.txt", "top.nfo"]


def test_empty_torrent_has_no_rows():
    assert build_file_tree([]) == []
    assert flatten_tree([], set()) == []


# -- live priority plumbing ------------------------------------------------- #


def test_files_are_all_wanted_by_default(manager, torrent_path):
    tid = manager.add_source(torrent_path)

    files = manager.files(tid)
    assert len(files) == len(FILE_LAYOUT)
    assert all(f.wanted for f in files)
    assert all(f.priority == FILE_PRIORITY_NORMAL for f in files)


def test_deselecting_a_file_skips_it_and_shrinks_the_wanted_size(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    files = manager.files(tid)
    target = index_of(files, "e01.bin")
    total = sum(f.size for f in files)

    assert manager.set_files_wanted(tid, [target], False) is True

    updated = {f.index: f for f in manager.files(tid)}
    assert updated[target].priority == FILE_PRIORITY_SKIP
    assert updated[target].wanted is False
    assert all(f.wanted for i, f in updated.items() if i != target)

    picked_bytes, total_bytes = manager.selected_bytes(tid)
    assert total_bytes == total
    assert picked_bytes == total - updated[target].size


def test_deselecting_a_folder_skips_every_file_under_it(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    season = find_dir(manager.file_tree(tid), "season1")

    assert manager.set_files_wanted(tid, season.indices, False) is True

    skipped = {f.index for f in manager.files(tid) if not f.wanted}
    assert skipped == set(season.indices)


def test_toggle_selects_a_partially_selected_group(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    season = find_dir(manager.file_tree(tid), "season1")
    manager.set_files_wanted(tid, season.indices[:1], False)

    # Partial -> everything on.
    manager.toggle_files_wanted(tid, season.indices)
    assert all(f.wanted for f in manager.files(tid) if f.index in season.indices)

    # Fully on -> everything off.
    manager.toggle_files_wanted(tid, season.indices)
    assert not any(f.wanted for f in manager.files(tid) if f.index in season.indices)


def test_select_all_and_none(manager, torrent_path):
    tid = manager.add_source(torrent_path)

    assert manager.set_all_files_wanted(tid, False) is True
    assert not any(f.wanted for f in manager.files(tid))

    assert manager.set_all_files_wanted(tid, True) is True
    assert all(f.wanted for f in manager.files(tid))


def test_selection_survives_a_restart(manager, torrent_path, tmp_path):
    tid = manager.add_source(torrent_path)
    target = index_of(manager.files(tid), "behind.bin")
    manager.set_files_wanted(tid, [target], False)

    # set_files_wanted asks for a checkpoint; drain alerts until it lands.
    deadline = 10
    while deadline and manager.store.load_resume(tid) is None:
        manager.session.wait_for_alert(500)
        manager.pump_alerts()
        deadline -= 1
    assert manager.store.load_resume(tid) is not None
    manager.session.pause()

    revived = SessionManager(
        Config(download_dir=str(tmp_path / "out"), listen_port=6904),
        store=manager.store,
    )
    try:
        revived.load_existing()
        files = {f.index: f for f in revived.files(tid)}
        assert files[target].wanted is False
        assert all(f.wanted for i, f in files.items() if i != target)
    finally:
        revived.session.pause()


def test_unknown_torrent_has_no_files(manager):
    assert manager.files("nope") == []
    assert manager.file_tree("nope") == []
    assert manager.set_files_wanted("nope", [0], False) is False
    assert manager.set_all_files_wanted("nope", True) is False
    assert manager.toggle_files_wanted("nope", [0]) is False


def test_endpoint_prioritization_leaves_skipped_files_alone(manager, torrent_path):
    tid = manager.add_source(torrent_path)
    handle = manager.torrents[tid].handle
    # Skip the file that owns piece 0 so the endpoint pass has to notice.
    first_file = handle.torrent_file().files().file_index_at_offset(0)
    manager.set_files_wanted(tid, [first_file], False)
    manager._ends_prioritized.discard(tid)

    manager.ensure_endpoints_prioritized()

    assert handle.piece_priority(0) == FILE_PRIORITY_SKIP
