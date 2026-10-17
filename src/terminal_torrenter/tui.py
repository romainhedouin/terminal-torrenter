#!/usr/bin/env python3
"""Textual terminal UI for terminal-torrenter.

Launch it to open a live dashboard showing every download at once. Navigate
rows with the arrow keys and act on the selected download with single-key
commands shown in the footer (add, rename, pause, remove, speed limits).

Pass a magnet link or .torrent file as an argument to add it on startup.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from . import __version__
from .config import (
    Config,
    ConfigError,
    config_path,
    state_dir,
    write_default_config,
)
from .core import (
    FileNode,
    InvalidSource,
    SessionManager,
    flatten_tree,
    human_bytes,
    human_duration,
    rarity_level,
)

try:
    import libtorrent as lt
except ImportError as exc:  # pragma: no cover
    raise SystemExit("libtorrent is required (pip install terminal-torrenter)") from exc


STATE_LABELS = {
    lt.torrent_status.states.checking_files: "checking",
    lt.torrent_status.states.downloading_metadata: "metadata",
    lt.torrent_status.states.downloading: "downloading",
    lt.torrent_status.states.finished: "done",
    lt.torrent_status.states.seeding: "seeding",
    lt.torrent_status.states.checking_resume_data: "checking",
}

PIECE_BLOCK = "█"
COLOR_HAVE = "#3b82f6"  # solid blue: downloaded
COLOR_DOWNLOADING = "#bfdbfe"  # pale blue: in progress
COLOR_MISSING = "#3a3a3a"  # dark grey: not downloaded yet
# Rarity levels 0..4 (rarest -> most common) for the optional overlay.
RARITY_COLORS = {0: "#ef4444", 1: "#f97316", 2: "#eab308", 3: "#84cc16", 4: "#22c55e"}

MARK_WANTED = "[✓]"
MARK_SKIPPED = "[ ]"
MARK_PARTIAL = "[~]"  # folder with only some of its files selected

FILE_COLUMN_SPECS = (
    ("Get", 5),
    ("Name", 50),
    ("Size", 11),
    ("Done", 8),
)

# (label, initial width). Columns still auto-grow to fit longer content.
COLUMN_SPECS = (
    ("Name", 34),
    ("Status", 12),
    ("Progress", 19),
    ("Size", 11),
    ("Down", 12),
    ("Up", 12),
    ("Peers/Seeds", 12),
    ("ETA", 12),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="terminal-torrenter",
        description="Interactive terminal torrent client (libtorrent + Textual).",
    )
    parser.add_argument("source", nargs="?", default=None,
                        help="Magnet link or .torrent file to add on startup.")
    # Flags default to None so an unset flag falls through to the config file;
    # see Config.merged_with_args.
    parser.add_argument("-d", "--download-dir", dest="download_dir", default=None,
                        help="Directory where files are saved.")
    parser.add_argument("--no-sequential", dest="sequential", action="store_false",
                        default=None, help="Disable sequential download.")
    parser.add_argument("--max-download-rate", type=int, default=None,
                        help="Download limit in KiB/s (0 = unlimited).")
    parser.add_argument("--max-upload-rate", type=int, default=None,
                        help="Upload limit in KiB/s (0 = unlimited).")
    parser.add_argument("--listen-port", type=int, default=None,
                        help="Local port libtorrent listens on for peers.")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help=f"Config file to read (default: {config_path()}).")
    parser.add_argument("--write-config", action="store_true",
                        help="Write a commented default config file and exit.")
    parser.add_argument("--force", action="store_true",
                        help="With --write-config, overwrite an existing file.")
    parser.add_argument("--show-config", action="store_true",
                        help="Print the resolved settings and exit.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Modal prompts
# --------------------------------------------------------------------------- #


class TextPrompt(ModalScreen[str | None]):
    """Single-line text input. Returns the value, or None if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, initial: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt)
            yield Input(value=self.initial, id="field")

    def on_mount(self) -> None:
        self.query_one("#field", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmPrompt(ModalScreen[bool]):
    """Yes/No confirmation. Returns True/False."""

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "No"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt)
            yield Label("[b]y[/b] Yes    [b]n[/b] No", id="hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class PieceMapScreen(Screen):
    """A heatmap of every piece in a torrent, with an optional rarity overlay."""

    CSS = """
    #summary { height: 1; padding: 0 1; color: $text; background: $panel; }
    #legend  { height: 1; padding: 0 1; }
    #piecemap { padding: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("r", "toggle_rarity", "Rarity overlay"),
    ]

    def __init__(self, manager: SessionManager, tid: str) -> None:
        super().__init__()
        self.manager = manager
        self.tid = tid
        self.show_rarity = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        yield Static("", id="legend")
        with VerticalScroll():
            yield Static("", id="piecemap")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Piece map"
        self.refresh_map()
        self.set_interval(0.5, self.refresh_map)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_toggle_rarity(self) -> None:
        self.show_rarity = not self.show_rarity
        self.refresh_map()

    def refresh_map(self) -> None:
        have, downloading, availability = self.manager.piece_state(self.tid)
        total = len(have)
        piece_map = self.query_one("#piecemap", Static)

        if total == 0:
            self.query_one("#summary", Static).update("Waiting for metadata…")
            self.query_one("#legend", Static).update("")
            piece_map.update("")
            return

        cols = max(1, piece_map.size.width or 80)
        text = Text(no_wrap=True)
        have_count = 0
        for index in range(total):
            if have[index]:
                style = COLOR_HAVE
                have_count += 1
            elif index in downloading:
                style = COLOR_DOWNLOADING
            elif self.show_rarity and index < len(availability):
                style = RARITY_COLORS[rarity_level(availability[index])]
            else:
                style = COLOR_MISSING
            text.append(PIECE_BLOCK, style=style)
            if (index + 1) % cols == 0:
                text.append("\n")
        piece_map.update(text)

        self._update_summary(have_count, total, len(downloading))
        self._update_legend()

    def _update_summary(self, have_count: int, total: int, downloading: int) -> None:
        name = self.manager.label_for(self.tid)
        percent = (have_count / total * 100) if total else 0.0
        piece_len = self.manager.piece_length(self.tid)
        size_note = f"  •  {human_bytes(piece_len)}/piece" if piece_len else ""
        self.query_one("#summary", Static).update(
            f"{name}  —  {have_count}/{total} pieces ({percent:.1f}%)"
            f"  •  downloading {downloading}{size_note}"
        )

    def _update_legend(self) -> None:
        legend = Text()
        legend.append(PIECE_BLOCK, style=COLOR_HAVE)
        legend.append(" downloaded   ")
        legend.append(PIECE_BLOCK, style=COLOR_DOWNLOADING)
        legend.append(" downloading   ")
        if self.show_rarity:
            legend.append("rarity ")
            for level, label in ((0, "0"), (1, "1"), (2, "2"), (3, "3-4"), (4, "5+")):
                legend.append(PIECE_BLOCK, style=RARITY_COLORS[level])
                legend.append(f" {label}  ")
            legend.append("peers   ·  press r to hide")
        else:
            legend.append(PIECE_BLOCK, style=COLOR_MISSING)
            legend.append(" remaining   ·  press r to show rarity")
        self.query_one("#legend", Static).update(legend)


class FileSelectScreen(Screen):
    """Pick which files and folders of a torrent to download.

    Toggling a row applies immediately: deselected files get priority 0, so
    libtorrent stops requesting them and the torrent's size, progress and ETA
    count only what is still selected.
    """

    CSS = """
    #files_summary { height: 1; padding: 0 1; color: $text; background: $panel; }
    #files_table { height: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("space", "toggle", "Select/skip"),
        Binding("e", "expand", "Fold/unfold"),
        Binding("a", "select_all", "All"),
        Binding("n", "select_none", "None"),
    ]

    def __init__(self, manager: SessionManager, tid: str) -> None:
        super().__init__()
        self.manager = manager
        self.tid = tid
        self.collapsed: set[str] = set()
        self.rows: list[FileNode] = []
        self.col_keys: list = []
        self.cell_cache: dict[str, tuple[str, ...]] = {}  # node path -> cells shown

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="files_summary")
        yield DataTable(id="files_table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Select files"
        table = self.query_one("#files_table", DataTable)
        self.col_keys = [
            table.add_column(label, width=width) for label, width in FILE_COLUMN_SPECS
        ]
        table.focus()
        self.refresh_files()
        self.set_interval(1.0, self.refresh_files)

    def action_close(self) -> None:
        self.dismiss(None)

    # -- rendering ---------------------------------------------------------- #

    def refresh_files(self) -> None:
        tree = self.manager.file_tree(self.tid)
        rows = flatten_tree(tree, self.collapsed)
        table = self.query_one("#files_table", DataTable)

        if not rows:
            self.rows = []
            table.clear()
            self.query_one("#files_summary", Static).update(
                "Waiting for metadata… the file list appears once it arrives."
            )
            return

        wanted = {f.index: f.wanted for f in self.manager.files(self.tid)}
        same_shape = [node.path for node in rows] == [node.path for node in self.rows]
        self.rows = rows

        if same_shape:
            # Only touch cells whose text changed. The columns have fixed widths,
            # so update_width stays off: with it, Textual re-measures the whole
            # column for every narrower cell, which is O(rows²) per refresh.
            for node in rows:
                cells = self._cells(node, wanted)
                if cells == self.cell_cache.get(node.path):
                    continue
                self.cell_cache[node.path] = cells
                for col_key, value in zip(self.col_keys, cells, strict=True):
                    table.update_cell(node.path, col_key, value)
        else:
            cursor = table.cursor_row
            table.clear()
            self.cell_cache = {}
            for node in rows:
                cells = self._cells(node, wanted)
                self.cell_cache[node.path] = cells
                table.add_row(*cells, key=node.path)
            if cursor is not None and 0 <= cursor < len(rows):
                table.move_cursor(row=cursor)

        self._update_summary(wanted)

    def _cells(self, node: FileNode, wanted: dict[int, bool]) -> tuple[str, ...]:
        picked = sum(1 for index in node.indices if wanted.get(index, True))
        if picked == 0:
            mark = MARK_SKIPPED
        elif picked == len(node.indices):
            mark = MARK_WANTED
        else:
            mark = MARK_PARTIAL
        prefix = "  " * node.depth
        if node.is_dir:
            prefix += "▸ " if node.path in self.collapsed else "▾ "
        else:
            prefix += "  "
        return (
            mark,
            f"{prefix}{node.name}",
            human_bytes(node.size),
            f"{node.progress * 100:5.1f}%",
        )

    def _update_summary(self, wanted: dict[int, bool]) -> None:
        picked_bytes, total_bytes = self.manager.selected_bytes(self.tid)
        picked = sum(1 for is_wanted in wanted.values() if is_wanted)
        self.query_one("#files_summary", Static).update(
            f"{self.manager.label_for(self.tid)}  —  {picked}/{len(wanted)} files"
            f"  •  {human_bytes(picked_bytes)} of {human_bytes(total_bytes)} selected"
        )

    # -- actions ------------------------------------------------------------ #

    def _selected_node(self) -> FileNode | None:
        if not self.rows:
            return None
        row = self.query_one("#files_table", DataTable).cursor_row
        if row is None or row < 0 or row >= len(self.rows):
            return None
        return self.rows[row]

    def action_toggle(self) -> None:
        node = self._selected_node()
        if node and self.manager.toggle_files_wanted(self.tid, node.indices):
            self.refresh_files()

    def action_expand(self) -> None:
        node = self._selected_node()
        if not node or not node.is_dir:
            return
        self.collapsed.symmetric_difference_update({node.path})
        self.refresh_files()

    def action_select_all(self) -> None:
        if self.manager.set_all_files_wanted(self.tid, True):
            self.refresh_files()
            self.notify("All files selected.")

    def action_select_none(self) -> None:
        if self.manager.set_all_files_wanted(self.tid, False):
            self.refresh_files()
            self.notify("All files skipped — nothing will be downloaded.", severity="warning")


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #


class TorrenterApp(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #table { height: 1fr; }
    #status_bar {
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    #dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #dialog Label { margin-bottom: 1; }
    TextPrompt, ConfirmPrompt { align: center middle; }
    """

    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("r", "rename", "Rename"),
        Binding("p", "pause", "Pause/Resume"),
        Binding("x", "remove", "Remove"),
        Binding("v", "view_pieces", "Piece map"),
        Binding("f", "select_files", "Files"),
        Binding("c", "recheck", "Verify"),
        Binding("s", "toggle_sequential", "Sequential"),
        Binding("d", "limit_down", "DL limit"),
        Binding("u", "limit_up", "UL limit"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, source: str | None = None) -> None:
        super().__init__()
        self.source = source
        self.manager = SessionManager(config)
        self.col_keys: list = []
        self.row_order: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        table = DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield table
        yield Static("", id="status_bar")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "terminal-torrenter"
        table = self.query_one("#table", DataTable)
        self.col_keys = [table.add_column(label, width=width) for label, width in COLUMN_SPECS]
        table.focus()

        self.manager.load_existing()
        if self.source:
            self._add_source(self.source)

        self.sync_rows()
        self.update_status_bar()
        self.set_interval(1.0, self._periodic)

    # -- periodic refresh --------------------------------------------------- #

    def _periodic(self) -> None:
        # Wrap so a transient error never kills the timer.
        try:
            self.manager.tick()
            self.sync_rows()
            self.update_status_bar()
        except Exception as exc:  # pragma: no cover - defensive
            self.notify(f"refresh error: {exc}", severity="error")

    def _cells(self, tid: str) -> tuple[str, ...]:
        managed = self.manager.torrents[tid]
        status = managed.handle.status()
        return (
            self.manager.label_for(tid),
            self._status_text(managed, status),
            self._bar(status.progress),
            human_bytes(status.total_wanted) if status.total_wanted else "—",
            f"{human_bytes(status.download_rate)}/s",
            f"{human_bytes(status.upload_rate)}/s",
            f"{status.num_peers}/{status.num_seeds}",
            human_duration(self.manager.eta_for(tid)),
        )

    def _status_text(self, managed, status) -> str:  # noqa: ANN001
        if managed.paused:
            return "paused"
        return STATE_LABELS.get(status.state, str(status.state))

    @staticmethod
    def _bar(progress: float, width: int = 10) -> str:
        filled = int(width * progress)
        return f"[{'#' * filled}{'-' * (width - filled)}] {progress * 100:5.1f}%"

    def sync_rows(self) -> None:
        table = self.query_one("#table", DataTable)
        order = list(self.manager.order)

        for tid in list(self.row_order):
            if tid not in order:
                try:
                    table.remove_row(tid)
                except Exception:
                    pass
                self.row_order.remove(tid)

        for tid in order:
            cells = self._cells(tid)
            if tid not in self.row_order:
                table.add_row(*cells, key=tid)
                self.row_order.append(tid)
            else:
                for col_key, value in zip(self.col_keys, cells, strict=True):
                    # update_width=True lets columns grow to fit content so
                    # values like "1 d 20 h" are never clipped to "1 d".
                    table.update_cell(tid, col_key, value, update_width=True)

    def update_status_bar(self) -> None:
        down, up = self.manager.total_rates()
        dl = f"{self.manager.dl_limit} KiB/s" if self.manager.dl_limit else "∞"
        ul = f"{self.manager.ul_limit} KiB/s" if self.manager.ul_limit else "∞"
        mode = "sequential" if self.manager.sequential else "rarest-first"
        count = len(self.manager.order)
        self.query_one("#status_bar", Static).update(
            f"↓ {human_bytes(down)}/s   ↑ {human_bytes(up)}/s   "
            f"│  limits ↓ {dl}  ↑ {ul}   │  mode: {mode}   │  torrents: {count}"
        )

    # -- selection ---------------------------------------------------------- #

    def _selected_tid(self) -> str | None:
        if not self.row_order:
            return None
        table = self.query_one("#table", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self.row_order):
            return None
        return self.row_order[row]

    # -- actions ------------------------------------------------------------ #

    def _add_source(self, source: str) -> None:
        try:
            self.manager.add_source(source)
        except InvalidSource as exc:
            self.notify(f"Could not add: {exc}", severity="error")

    def action_view_pieces(self) -> None:
        if isinstance(self.screen, PieceMapScreen):
            return  # already open; don't stack another
        tid = self._selected_tid()
        if tid:
            self.push_screen(PieceMapScreen(self.manager, tid))

    def action_select_files(self) -> None:
        if isinstance(self.screen, FileSelectScreen):
            return  # already open; don't stack another
        tid = self._selected_tid()
        if not tid:
            return
        if not self.manager.files(tid):
            self.notify("No file list yet — still fetching metadata.", severity="warning")
            return
        self.push_screen(FileSelectScreen(self.manager, tid))

    def action_toggle_sequential(self) -> None:
        sequential = self.manager.toggle_sequential()
        self.update_status_bar()
        self.notify(f"Sequential download {'ON' if sequential else 'OFF'} for all downloads.")

    def action_add(self) -> None:
        def done(value: str | None) -> None:
            if value and value.strip():
                self._add_source(value.strip())
                self.sync_rows()
        self.push_screen(TextPrompt("Add magnet link or .torrent path:"), done)

    def action_rename(self) -> None:
        tid = self._selected_tid()
        if not tid:
            return
        current = self.manager.label_for(tid)

        def done(value: str | None) -> None:
            if value is not None and value.strip():
                self.manager.rename(tid, value.strip())
                self.sync_rows()
                self.notify("Renamed.")
        self.push_screen(TextPrompt("New name:", initial=current), done)

    def action_pause(self) -> None:
        tid = self._selected_tid()
        if tid:
            self.manager.toggle_pause(tid)
            self.sync_rows()

    def action_recheck(self) -> None:
        tid = self._selected_tid()
        if not tid:
            return
        label = self.manager.label_for(tid)

        def done(confirmed: bool) -> None:
            if confirmed and self.manager.recheck(tid):
                self.sync_rows()
                self.notify(f"Verifying “{label}”… (re-hashing all pieces)")
        self.push_screen(
            ConfirmPrompt(
                f"Verify “{label}”?\nThis re-hashes all pieces on disk and may take a while."
            ),
            done,
        )

    def action_remove(self) -> None:
        tid = self._selected_tid()
        if not tid:
            return
        label = self.manager.label_for(tid)

        def done(confirmed: bool) -> None:
            if confirmed:
                self.manager.remove(tid)
                self.sync_rows()
                self.notify(f"Removed “{label}” (files kept on disk).")
        self.push_screen(
            ConfirmPrompt(f"Remove “{label}” from the list?\nFiles already on disk are kept."),
            done,
        )

    def action_limit_down(self) -> None:
        def done(value: str | None) -> None:
            limit = self._parse_limit(value)
            if limit is not None:
                self.manager.set_download_limit(limit)
                self.update_status_bar()
                self.notify(f"Download limit: {limit or 'unlimited'} KiB/s")
        self.push_screen(
            TextPrompt(
                "Download limit in KiB/s (0 = unlimited):",
                initial=str(self.manager.dl_limit),
            ),
            done,
        )

    def action_limit_up(self) -> None:
        def done(value: str | None) -> None:
            limit = self._parse_limit(value)
            if limit is not None:
                self.manager.set_upload_limit(limit)
                self.update_status_bar()
                self.notify(f"Upload limit: {limit or 'unlimited'} KiB/s")
        self.push_screen(
            TextPrompt(
                "Upload limit in KiB/s (0 = unlimited):",
                initial=str(self.manager.ul_limit),
            ),
            done,
        )

    def _parse_limit(self, value: str | None) -> int | None:
        if value is None:
            return None
        try:
            return max(0, int(value.strip()))
        except ValueError:
            self.notify("Please enter a whole number.", severity="error")
            return None

    def action_quit(self) -> None:
        self.manager.shutdown()
        self.exit()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.write_config:
        try:
            print(f"Wrote {write_default_config(args.config, force=args.force)}")
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        config = Config.load(args.config).merged_with_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.show_config:
        print(f"# from {args.config or config_path()}")
        for name, value in asdict(config).items():
            print(f"{name} = {json.dumps(value)}")
        print(f"# state_dir = {json.dumps(state_dir())}")
        return 0

    TorrenterApp(config, args.source).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
