"""Re-adding a source you already hold merges its trackers and web seeds.

libtorrent hands back the existing handle and discards the new
add_torrent_params, so SessionManager folds the announces in by hand.
"""

import os
import time

import libtorrent as lt
import pytest

from terminal_torrenter.config import Config
from terminal_torrenter.core import SessionManager, TorrentStore

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
OTHER_HASH = "89abcdef0123456789abcdef0123456789abcdef"


def magnet(info_hash=INFO_HASH, trackers=(), seeds=()):
    uri = f"magnet:?xt=urn:btih:{info_hash}"
    uri += "".join(f"&tr={t}" for t in trackers)
    uri += "".join(f"&ws={s}" for s in seeds)
    return uri


def make_torrent(tmp_path, content_name, out_name, private, tracker, seed):
    """Build a .torrent. Same content_name -> same info hash, by construction."""
    content = tmp_path / content_name
    content.mkdir(exist_ok=True)
    (content / "f.bin").write_bytes(bytes([len(content_name)]) * 32768)
    fs = lt.file_storage()
    lt.add_files(fs, str(content))
    creator = lt.create_torrent(fs)
    creator.set_priv(private)
    creator.add_tracker(tracker, 0)
    creator.add_url_seed(seed)
    lt.set_piece_hashes(creator, str(tmp_path))
    path = tmp_path / out_name
    path.write_bytes(lt.bencode(creator.generate()))
    return str(path)


@pytest.fixture
def manager(tmp_path):
    mgr = SessionManager(
        Config(download_dir=str(tmp_path / "out"), listen_port=6907),
        store=TorrentStore(str(tmp_path / "state")),
    )
    yield mgr
    mgr.session.pause()


def wait_for_resume(manager, tid, timeout=10.0):
    """Pump alerts until the resume blob for `tid` is on disk; return its path."""
    path = os.path.join(manager.store.directory, tid + ".resume")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manager.pump_alerts()
        if os.path.isfile(path):
            return path
        time.sleep(0.02)
    raise AssertionError(f"resume data for {tid} never arrived within {timeout}s")


def trackers_of(manager, tid):
    return sorted(entry["url"] for entry in manager.torrents[tid].handle.trackers())


def seeds_of(manager, tid):
    return sorted(manager.torrents[tid].handle.url_seeds())


def test_new_trackers_are_merged_not_discarded(manager):
    tid = manager.add_source(magnet(trackers=["udp://a:80"], seeds=["http://a/"]))
    again = manager.add_source(magnet(trackers=["udp://b:80"], seeds=["http://b/"]))

    assert again == tid, "same info hash must not create a second torrent"
    assert len(manager.torrents) == 1
    assert trackers_of(manager, tid) == ["udp://a:80", "udp://b:80"]
    assert seeds_of(manager, tid) == ["http://a/", "http://b/"]


def test_duplicate_trackers_are_not_added_twice(manager):
    tid = manager.add_source(magnet(trackers=["udp://a:80"]))
    manager.add_source(magnet(trackers=["udp://a:80", "udp://a:80"]))
    assert trackers_of(manager, tid) == ["udp://a:80"]


def test_merge_survives_the_resume_data_path(manager, tmp_path):
    """A re-add after a checkpoint takes the resume branch; merge still applies."""
    tid = manager.add_source(magnet(trackers=["udp://a:80"]))
    manager.request_checkpoints()
    wait_for_resume(manager, tid)

    manager.add_source(magnet(trackers=["udp://c:80"]))
    assert trackers_of(manager, tid) == ["udp://a:80", "udp://c:80"]


def test_merged_trackers_reach_the_resume_file(manager):
    tid = manager.add_source(magnet(trackers=["udp://a:80"]))
    manager.add_source(magnet(trackers=["udp://b:80"]))
    path = wait_for_resume(manager, tid)
    with open(path, "rb") as fh:
        saved = lt.read_resume_data(fh.read())
    assert sorted(saved.trackers) == ["udp://a:80", "udp://b:80"]


def test_different_info_hash_stays_a_separate_torrent(manager):
    tid = manager.add_source(magnet(trackers=["udp://a:80"]))
    other = manager.add_source(magnet(OTHER_HASH, trackers=["udp://z:80"]))
    assert other != tid
    assert len(manager.torrents) == 2
    assert trackers_of(manager, tid) == ["udp://a:80"]


def test_torrent_file_sources_merge_too(manager, tmp_path):
    first = make_torrent(tmp_path, "pub", "a.torrent", False, "udp://file-a:80", "http://fs-a/")
    tid = manager.add_source(first)
    # Same content, different announce metadata -> same info hash.
    second = make_torrent(tmp_path, "pub", "b.torrent", False, "udp://file-b:80", "http://fs-b/")
    assert manager.add_source(second) == tid
    assert trackers_of(manager, tid) == ["udp://file-a:80", "udp://file-b:80"]
    assert seeds_of(manager, tid) == ["http://fs-a/", "http://fs-b/"]


def test_private_torrents_are_left_alone(manager, tmp_path):
    """BEP 27: a private torrent must only use the trackers it shipped with."""
    path = make_torrent(tmp_path, "priv", "p.torrent", True, "udp://private:80", "http://ps/")
    tid = manager.add_source(path)
    assert manager.torrents[tid].handle.torrent_file().priv()

    info_hash = str(manager.torrents[tid].handle.status().info_hashes.v1)
    manager.add_source(magnet(info_hash, trackers=["udp://public-evil:80"]))

    assert trackers_of(manager, tid) == ["udp://private:80"]
    assert seeds_of(manager, tid) == ["http://ps/"]
