"""Core torrent logic: persistent state store and a libtorrent session manager.

This module is UI-agnostic; the TUI in ``tui.py`` drives it.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields

from .config import Config, state_dir

try:
    import libtorrent as lt
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Error: the 'libtorrent' package is required but not installed.\n"
        "Install it with:  pip install terminal-torrenter\n"
        "On macOS you may also use:  brew install libtorrent-rasterbar"
    ) from exc


RESUME_SAVE_INTERVAL = 15  # seconds between fast-resume checkpoints
ETA_WINDOW_SECONDS = 10.0  # download-rate is averaged over this window for ETA


class MovingAverage:
    """Time-windowed average of recent samples (used to smooth the ETA).

    Samples are kept for ``window_seconds``; older ones are discarded.
    """

    def __init__(self, window_seconds: float = ETA_WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self._samples: deque[tuple[float, float]] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def add(self, value: float, now: float) -> None:
        self._samples.append((now, value))
        self._prune(now)

    def average(self, now: float | None = None) -> float:
        if now is not None:
            self._prune(now)
        if not self._samples:
            return 0.0
        return sum(value for _, value in self._samples) / len(self._samples)


def human_bytes(num: float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:3.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def eta_seconds(total_bytes: int, done_bytes: int, rate_bytes_per_s: float) -> float:
    """Seconds until completion at the current rate.

    Returns ``inf`` when it cannot be estimated (no progress info, zero/negative
    rate, or already complete).
    """
    if rate_bytes_per_s <= 0 or total_bytes <= 0:
        return float("inf")
    remaining = total_bytes - done_bytes
    if remaining <= 0:
        return float("inf")
    return remaining / rate_bytes_per_s


def human_duration(seconds: float) -> str:
    """Human ETA using the two most significant units, e.g. "1 d 20 h",
    "15 h 42 min", "2 min 50 s", or "50 s"."""
    if seconds <= 0 or seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def downloading_pieces_from_queue(queue) -> set[int]:
    """Extract the set of in-flight piece indices from libtorrent's download
    queue. Queue items are dicts (``{"piece_index": ...}``) in current
    libtorrent, but older versions expose objects; handle both."""
    result: set[int] = set()
    for partial in queue:
        if isinstance(partial, dict):
            index = partial.get("piece_index")
        else:
            index = getattr(partial, "piece_index", None)
        if index is not None:
            result.add(int(index))
    return result


def rarity_level(availability: int) -> int:
    """Bucket a piece's availability (how many connected peers have it) into a
    rarity level 0..4, where 0 is rarest (nobody has it) and 4 is abundant."""
    if availability <= 0:
        return 0
    if availability == 1:
        return 1
    if availability == 2:
        return 2
    if availability <= 4:
        return 3
    return 4


def info_hashes_to_id(info_hashes: lt.info_hash_t) -> str | None:
    if info_hashes.has_v1():
        return str(info_hashes.v1)
    if info_hashes.has_v2():
        return str(info_hashes.v2)
    return None


# --------------------------------------------------------------------------- #
# Persistent state
# --------------------------------------------------------------------------- #


@dataclass
class DownloadEntry:
    id: str
    source: str
    save_path: str
    sequential: bool = True
    name: str = ""
    display_name: str = ""
    paused: bool = False
    progress: float = 0.0
    total_bytes: int = 0
    finished: bool = False
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> DownloadEntry:
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class TorrentStore:
    """Persists per-download state under the state directory.

    Per torrent (keyed by info-hash):
      <id>.resume  libtorrent fast-resume blob (progress + embedded metadata)
      <id>.json    sidecar describing the download for the manager list
    """

    def __init__(self, directory: str | None = None) -> None:
        self.directory = directory or state_dir()
        os.makedirs(self.directory, exist_ok=True)

    def _resume_path(self, tid: str) -> str:
        return os.path.join(self.directory, f"{tid}.resume")

    def _meta_path(self, tid: str) -> str:
        return os.path.join(self.directory, f"{tid}.json")

    def save_meta(self, entry: DownloadEntry) -> None:
        tmp = self._meta_path(entry.id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(entry), fh, indent=2)
        os.replace(tmp, self._meta_path(entry.id))

    def save_resume(self, tid: str, resume_buf: bytes) -> None:
        tmp = self._resume_path(tid) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(resume_buf)
        os.replace(tmp, self._resume_path(tid))

    def load_resume(self, tid: str) -> bytes | None:
        path = self._resume_path(tid)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
        return None

    def list(self) -> list[DownloadEntry]:
        entries: list[DownloadEntry] = []
        for name in os.listdir(self.directory):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.directory, name), encoding="utf-8") as fh:
                    entries.append(DownloadEntry.from_dict(json.load(fh)))
            except (json.JSONDecodeError, TypeError, OSError):
                continue
        entries.sort(key=lambda e: e.updated_at)
        return entries

    def remove(self, tid: str) -> None:
        for path in (self._resume_path(tid), self._meta_path(tid)):
            if os.path.isfile(path):
                os.remove(path)


# --------------------------------------------------------------------------- #
# Session manager
# --------------------------------------------------------------------------- #


@dataclass
class ManagedTorrent:
    handle: lt.torrent_handle
    source: str
    sequential: bool
    display_name: str = ""
    paused: bool = False


class InvalidSource(ValueError):
    pass


def _apply_sequential(params: lt.add_torrent_params, sequential: bool) -> None:
    if sequential:
        params.flags |= lt.torrent_flags.sequential_download
    else:
        params.flags &= ~lt.torrent_flags.sequential_download


def announces_from_params(params: lt.add_torrent_params) -> tuple[list[str], list[str]]:
    """Trackers and web seeds carried by a source, whether it was a magnet
    (they land in the params) or a .torrent file (they live in the info)."""
    trackers = list(params.trackers)
    seeds = list(params.url_seeds)
    if params.ti:
        trackers += [entry.url for entry in params.ti.trackers()]
        seeds += [ws["url"] for ws in params.ti.web_seeds()]
    return trackers, seeds


def params_from_source(source: str, save_path: str, sequential: bool) -> lt.add_torrent_params:
    if source.startswith("magnet:"):
        params = lt.parse_magnet_uri(source)
    elif source.startswith(("http://", "https://")):
        params = lt.add_torrent_params()
        params.url = source
    else:
        if not os.path.isfile(source):
            raise InvalidSource(f"torrent file not found: {source}")
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(source)
    params.save_path = save_path
    _apply_sequential(params, sequential)
    return params


class SessionManager:
    """Owns a single libtorrent session holding every managed torrent."""

    def __init__(
        self,
        config: Config | None = None,
        store: TorrentStore | None = None,
    ) -> None:
        self.config = config or Config()
        self.output_dir = self.config.download_dir
        os.makedirs(self.output_dir, exist_ok=True)
        self.store = store or TorrentStore()
        self.dl_limit = self.config.max_download_rate  # KiB/s, 0 = unlimited
        self.ul_limit = self.config.max_upload_rate
        self.sequential = self.config.sequential  # global, applies to every torrent
        self.session = lt.session(
            {
                "listen_interfaces": f"0.0.0.0:{self.config.listen_port}",
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": True,
                "enable_natpmp": True,
                "announce_to_all_trackers": True,
                "announce_to_all_tiers": True,
                "download_rate_limit": self.dl_limit * 1024,
                "upload_rate_limit": self.ul_limit * 1024,
                "user_agent": "downloader/2.0 libtorrent/" + lt.version,
            }
        )
        self.torrents: dict[str, ManagedTorrent] = {}
        self.order: list[str] = []
        self._last_checkpoint = 0.0
        self._ends_prioritized: set[str] = set()
        self._rate_avg: dict[str, MovingAverage] = {}

    # -- adding / restoring ------------------------------------------------- #

    def load_existing(self) -> None:
        for entry in self.store.list():
            try:
                self._add_entry(entry)
            except InvalidSource:
                continue

    def _add_entry(self, entry: DownloadEntry) -> None:
        resume_buf = self.store.load_resume(entry.id)
        save_path = entry.save_path or self.output_dir
        if resume_buf:
            params = lt.read_resume_data(resume_buf)
            params.save_path = save_path
            _apply_sequential(params, self.sequential)
        else:
            params = params_from_source(entry.source, save_path, self.sequential)
        if entry.paused:
            # Clear auto_managed too, otherwise the queue manager resumes the
            # torrent on startup despite it being saved as paused.
            params.flags |= lt.torrent_flags.paused
            params.flags &= ~lt.torrent_flags.auto_managed
        handle = self.session.add_torrent(params)
        tid = info_hashes_to_id(handle.status().info_hashes) or entry.id
        self.torrents[tid] = ManagedTorrent(
            handle=handle,
            source=entry.source,
            sequential=self.sequential,
            display_name=entry.display_name,
            paused=entry.paused,
        )
        if tid not in self.order:
            self.order.append(tid)

    def add_source(self, source: str, save_path: str | None = None) -> str:
        source = source.strip()
        if not source:
            raise InvalidSource("empty source")
        save_path = save_path or self.output_dir
        provisional = params_from_source(source, save_path, self.sequential)
        tid = info_hashes_to_id(provisional.info_hashes)

        resume_buf = self.store.load_resume(tid) if tid else None
        if resume_buf:
            params = lt.read_resume_data(resume_buf)
            params.save_path = save_path
            _apply_sequential(params, self.sequential)
        else:
            params = provisional

        handle = self.session.add_torrent(params)
        tid = info_hashes_to_id(handle.status().info_hashes) or source
        if tid not in self.torrents:
            self.torrents[tid] = ManagedTorrent(
                handle=handle, source=source, sequential=self.sequential
            )
            self.order.append(tid)
            self._persist_meta(tid)
        else:
            # Re-adding a torrent we already hold: libtorrent hands back the
            # existing handle and drops the new params on the floor, so any
            # trackers/web seeds the second source knew about would be lost.
            # Fold them in by hand. Safe because the id is the info hash, so
            # "already present" means byte-identical content.
            self._merge_announces(tid, provisional)
        return tid

    def _merge_announces(self, tid: str, provisional: lt.add_torrent_params) -> None:
        """Add trackers/web seeds from a duplicate source to the live torrent."""
        managed = self.torrents[tid]
        handle = managed.handle
        if not handle.is_valid():
            return
        info = handle.torrent_file()
        if info is not None and info.priv():
            # BEP 27: a private torrent must only use the trackers it shipped
            # with. Grafting extra ones on risks a ban from the originating
            # tracker, so leave it alone.
            return

        trackers, seeds = announces_from_params(provisional)
        known_trackers = {entry["url"] for entry in handle.trackers()}
        known_seeds = set(handle.url_seeds())
        added = 0
        for url in trackers:
            if url not in known_trackers:
                handle.add_tracker({"url": url, "tier": 0})
                known_trackers.add(url)
                added += 1
        for url in seeds:
            if url not in known_seeds:
                handle.add_url_seed(url)
                known_seeds.add(url)
                added += 1
        if added:
            # Checkpoint now so the merged list survives a restart instead of
            # waiting on the next interval (and losing it if we exit first).
            handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)

    # -- actions ------------------------------------------------------------ #

    def rename(self, tid: str, new_name: str) -> None:
        if tid in self.torrents:
            self.torrents[tid].display_name = new_name
            self._persist_meta(tid)

    def toggle_pause(self, tid: str) -> None:
        managed = self.torrents.get(tid)
        if not managed:
            return
        if managed.paused:
            # Hand control back to the auto-manager and resume.
            managed.handle.set_flags(lt.torrent_flags.auto_managed)
            managed.handle.resume()
            managed.paused = False
        else:
            # An auto-managed torrent is resumed by libtorrent's queue manager
            # right after we pause it, so the pause never sticks (the torrent
            # keeps downloading while the UI shows "paused"). Clear the
            # auto_managed flag first so the pause actually holds.
            managed.handle.unset_flags(lt.torrent_flags.auto_managed)
            managed.handle.pause()
            managed.paused = True
        self._persist_meta(tid)

    def remove(self, tid: str, delete_files: bool = False) -> None:
        managed = self.torrents.pop(tid, None)
        if tid in self.order:
            self.order.remove(tid)
        self._rate_avg.pop(tid, None)
        self._ends_prioritized.discard(tid)
        if managed:
            try:
                if delete_files:
                    self.session.remove_torrent(managed.handle, lt.session.delete_files)
                else:
                    self.session.remove_torrent(managed.handle)
            except Exception:
                pass
        self.store.remove(tid)

    def set_sequential_all(self, sequential: bool) -> None:
        """Toggle sequential download for every torrent at once."""
        self.sequential = sequential
        for tid, managed in self.torrents.items():
            if sequential:
                managed.handle.set_flags(lt.torrent_flags.sequential_download)
            else:
                managed.handle.unset_flags(lt.torrent_flags.sequential_download)
            managed.sequential = sequential
            self._persist_meta(tid)

    def toggle_sequential(self) -> bool:
        self.set_sequential_all(not self.sequential)
        return self.sequential

    def ensure_endpoints_prioritized(self) -> None:
        """Make sure the first and last piece of every torrent are fetched
        early, regardless of sequential mode (handy for media headers/footers
        and quick verification)."""
        for tid, managed in self.torrents.items():
            if tid in self._ends_prioritized:
                continue
            info = managed.handle.torrent_file()
            if info is None:
                continue  # metadata not available yet
            num_pieces = info.num_pieces()
            if num_pieces <= 0:
                continue
            for index in {0, num_pieces - 1}:
                managed.handle.piece_priority(index, 7)  # 7 = top priority
                managed.handle.set_piece_deadline(index, 0)  # fetch ASAP
            self._ends_prioritized.add(tid)

    def recheck(self, tid: str) -> bool:
        """Force a full re-hash of the torrent's files against the piece
        hashes (verifies on-disk data). Returns True if a recheck was started.
        """
        managed = self.torrents.get(tid)
        if not managed or not managed.handle.is_valid():
            return False
        managed.handle.force_recheck()
        # Pieces may flip back to "not have" during the check; let the endpoint
        # prioritization re-apply afterwards.
        self._ends_prioritized.discard(tid)
        return True

    def set_download_limit(self, kib_per_s: int) -> None:
        self.dl_limit = max(0, kib_per_s)
        self.session.apply_settings({"download_rate_limit": self.dl_limit * 1024})

    def set_upload_limit(self, kib_per_s: int) -> None:
        self.ul_limit = max(0, kib_per_s)
        self.session.apply_settings({"upload_rate_limit": self.ul_limit * 1024})

    # -- persistence -------------------------------------------------------- #

    def label_for(self, tid: str) -> str:
        managed = self.torrents.get(tid)
        if not managed:
            return tid
        if managed.display_name:
            return managed.display_name
        name = managed.handle.status().name
        return name or "(fetching metadata…)"

    def _entry_for(self, tid: str) -> DownloadEntry:
        managed = self.torrents[tid]
        status = managed.handle.status()
        return DownloadEntry(
            id=tid,
            source=managed.source,
            save_path=status.save_path or self.output_dir,
            sequential=managed.sequential,
            name=status.name or "",
            display_name=managed.display_name,
            paused=managed.paused,
            progress=status.progress,
            total_bytes=status.total_wanted,
            finished=bool(status.is_finished or status.is_seeding),
            updated_at=time.time(),
        )

    def _persist_meta(self, tid: str) -> None:
        if tid in self.torrents:
            self.store.save_meta(self._entry_for(tid))

    def pump_alerts(self) -> None:
        """Drain libtorrent alerts; write resume blobs as they arrive."""
        for alert in self.session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                tid = info_hashes_to_id(alert.params.info_hashes)
                if tid:
                    self.store.save_resume(tid, lt.write_resume_data_buf(alert.params))
                    self._persist_meta(tid)

    def piece_state(self, tid: str) -> tuple[list[bool], set[int], list[int]]:
        """Return per-piece state for the heatmap:
        (have, downloading_indices, availability_per_piece)."""
        managed = self.torrents.get(tid)
        if not managed:
            return [], set(), []
        status = managed.handle.status()
        have = list(status.pieces) if status.pieces else []
        downloading = downloading_pieces_from_queue(managed.handle.get_download_queue())
        availability = list(managed.handle.piece_availability())
        return have, downloading, availability

    def piece_length(self, tid: str) -> int:
        managed = self.torrents.get(tid)
        if managed:
            info = managed.handle.torrent_file()
            if info is not None:
                return info.piece_length()
        return 0

    def smoothed_download_rate(self, tid: str) -> float:
        averager = self._rate_avg.get(tid)
        return averager.average(time.time()) if averager else 0.0

    def eta_for(self, tid: str) -> float:
        managed = self.torrents.get(tid)
        if not managed:
            return float("inf")
        status = managed.handle.status()
        return eta_seconds(
            status.total_wanted, status.total_wanted_done, self.smoothed_download_rate(tid)
        )

    def tick(self) -> None:
        """Call ~once per second from the UI loop."""
        self.pump_alerts()
        self.ensure_endpoints_prioritized()
        now = time.time()
        for tid, managed in self.torrents.items():
            rate = managed.handle.status().download_rate
            self._rate_avg.setdefault(tid, MovingAverage()).add(rate, now)
        for tid in list(self.torrents):
            self._persist_meta(tid)
        if now - self._last_checkpoint >= RESUME_SAVE_INTERVAL:
            self.request_checkpoints()
            self._last_checkpoint = now

    def request_checkpoints(self) -> None:
        for managed in self.torrents.values():
            if managed.handle.is_valid():
                managed.handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)

    def total_rates(self) -> tuple[int, int]:
        down = up = 0
        for managed in self.torrents.values():
            status = managed.handle.status()
            down += status.download_rate
            up += status.upload_rate
        return down, up

    def shutdown(self) -> None:
        self.session.pause()
        self.request_checkpoints()
        # Give resume-data alerts a few moments to arrive, then flush them.
        deadline = time.time() + 3
        while time.time() < deadline and self.torrents:
            self.session.wait_for_alert(300)
            self.pump_alerts()
        for tid in list(self.torrents):
            self._persist_meta(tid)
