# agnostic/

Scripts in this folder are intended to run unmodified on Linux, Windows, and macOS — no OS-specific binaries, paths, or shell calls, just Python 3 (and its standard library unless a script says otherwise).

> **Flag:** `video-processor.py` is listed below because it currently lives in this folder, but it is **not actually platform-agnostic yet** — see its entry for details. Planned: refactor it to run cross-platform while keeping the existing Android/Termux path intact as one branch, since that's the primary use case.

## Scripts

### `text-search.py`

Searches the human-readable text inside files for a keyword or regex pattern — including inside binary files (via printable-string extraction, like `strings`) and `.torrent` files (via a built-in bencode parser that reads name/comment/announce-URL fields while skipping the binary `pieces` hash blob).

- **Requirements:** Python 3, standard library only
- **Platform notes:** Genuinely OS-agnostic — pure Python, no external processes or OS-specific paths
- **Usage:**
  ```bash
  # Search current directory, non-recursive
  python3 text-search.py "ubuntu"

  # Recursive search, only .torrent files
  python3 text-search.py "ubuntu" -r --include-ext torrent

  # Recursive search, skip a cache folder
  python3 text-search.py "ubuntu" -r --exclude-path "*/cache/*"

  # Case-sensitive search of a specific directory
  python3 text-search.py "Ubuntu" -p /path/to/dir --case-sensitive
  ```
- **Key options:** `-r/--recursive`, `--include-ext` / `--exclude-ext` (extension whitelist/blacklist), `--include-path` / `--exclude-path` (glob whitelist/blacklist), `--case-sensitive`, `--regex`, `-q/--quiet`
- Run with no keyword argument and it will prompt for one interactively.

---

### `video-processor.py`

Interactive video transcoder built around `ffmpeg`/`ffprobe`, with menus for resolution, codec, and quality/size targeting (fixed size cap, constant-quality, or a flat default bitrate). Probes the source first for duration, bitrate, and HDR color transfer, cleans up partial output files on `Ctrl+C`, and shows live progress during encoding.

- **⚠️ Not actually agnostic yet:** it encodes via `h264_mediacodec` / `hevc_mediacodec` / `av1_mediacodec` / `vp9_mediacodec` / `vp8_mediacodec` — `ffmpeg`'s wrapper around **Android's MediaCodec hardware encoder API**. The script's own comments describe workarounds specifically for Termux/Android (e.g. running the progress loop synchronously "to avoid Termux/Android Python threading overhead"). It will not run as-is on desktop Linux, Windows, or macOS.
- **Planned:** refactor to detect platform and offer other encoders (e.g. libx264/libx265 or platform-native hardware encoders) on non-Android systems, while keeping the Termux/MediaCodec path as-is — that's still the primary use case. A couple of known bugs are also slated to be fixed as part of this pass.
- **Requirements:** Termux on Android, `ffmpeg` built with MediaCodec support, `ffprobe`
- **Usage:**
  ```bash
  python video-processor.py [input_file] [--dry-run]
  python video-processor.py               # prompts for the input path
  ```
- **Notable behavior:**
  - `--dry-run` prints the constructed `ffmpeg` command without executing it
  - Warns (but doesn't block) on HDR sources, since MediaCodec encodes to SDR without tone-mapping
  - Decoding is deliberately left on CPU — hardware decode caused memory-sync artifacts and was slower in testing
  - Subtitles are skipped entirely for WebM output (container only supports WebVTT)
