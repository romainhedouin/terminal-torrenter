# terminal-torrenter

An interactive terminal torrent client built on
[libtorrent](https://www.libtorrent.org/) and [Textual](https://textual.textualize.io/).
One live dashboard for every download: arrow keys to select, single keys to act.
Downloads resume automatically next time you open it.

```
┌ terminal-torrenter ───────────────────────────────────────────────────────┐
│ Name        Status       Progress          Size     Down       Peers/Seeds│
│ Sintel      downloading  [#####-----] 42%  123 MiB  1.4 MiB/s  31/29      │
│ Big Buck…   seeding      [##########]100%  350 MiB  0.0 B/s    0/4        │
│ Ubuntu ISO  paused       [###-------] 25%  3.8 GiB  0.0 B/s    0/0        │
├───────────────────────────────────────────────────────────────────────────┤
│ ↓ 1.4 MiB/s  ↑ 82 KiB/s │ limits ↓ ∞ ↑ ∞ │ torrents: 3                    │
│ a Add  r Rename  p Pause  x Remove  v Pieces  c Verify  q Quit            │
└───────────────────────────────────────────────────────────────────────────┘
```

## Install

```bash
pip install git+https://github.com/romainhedouin/terminal-torrenter
```

If no `libtorrent` wheel exists for your platform, install the native library
first (`brew install libtorrent-rasterbar`, or `apt install python3-libtorrent`).

## Use

```bash
tt                                  # open the dashboard
tt "magnet:?xt=urn:btih:..."        # open it and add a download
tt path/to/file.torrent
tt -d ~/Movies --max-download-rate 2048
```

`terminal-torrenter` works as a longer alias for `tt`.

| Key | Action | | Key | Action |
| --- | --- | --- | --- | --- |
| `↑` `↓` | Select | | `v` | Piece heatmap (`r` toggles rarity) |
| `a` | Add magnet / `.torrent` | | `c` | Verify (re-hash pieces on disk) |
| `r` | Rename (label only) | | `s` | Toggle sequential download |
| `p` | Pause / resume | | `d` `u` | Download / upload limit |
| `x` | Remove (keeps files) | | `q` | Quit |

## Configure

Settings live in `~/.config/terminal-torrenter/config.toml`. Create one
pre-filled with the defaults:

```bash
tt --write-config     # then edit it; tt --show-config prints what's in effect
```

```toml
download_dir = "~/Documents/downloads"   # where files are saved
sequential = true                        # download pieces in order
listen_port = 6881
max_download_rate = 0                    # KiB/s, 0 = unlimited
max_upload_rate = 0
```

Command-line flags override the file, which overrides these defaults.

Resume data lives in `~/.local/state/terminal-torrenter/` (one `.resume` blob
and one `.json` sidecar per torrent). `XDG_CONFIG_HOME` and `XDG_STATE_HOME` are
respected; `TERMINAL_TORRENTER_CONFIG` and `TERMINAL_TORRENTER_STATE_DIR`
override either location outright.

## Notes

- Sequential mode is on by default, and the first and last piece of every
  torrent are always prioritised — handy for previewing media as it downloads.
- Re-adding a magnet you already have merges any new trackers and web seeds into
  the existing torrent instead of discarding them. Private torrents (BEP 27) are
  deliberately left untouched.
- Run one instance at a time; two sharing a state directory will conflict.
- Peer discovery uses DHT, LSD, UPnP and NAT-PMP.
- Only download content you have the right to access.

## Develop

```bash
pip install -e ".[dev]"
pytest && ruff check .
```

`core.py` holds the UI-agnostic session logic, `tui.py` the Textual app,
`config.py` the config file.

## License

MIT — see [LICENSE](LICENSE).
