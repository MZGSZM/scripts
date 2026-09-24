# agnostic/

Scripts in this folder are intended to run unmodified on Linux, Windows, and macOS: no OS-specific binaries, paths, or shell calls, just Python 3 (and its standard library unless a script says otherwise).

> **Flag:** `video-processor.py` is listed below because it currently lives in this folder, but it is **not actually platform-agnostic yet** (see its entry for details). Planned: refactor it to run cross-platform while keeping the existing Android/Termux path intact as one branch, since that's the primary use case.

## Scripts

### `text-search.py`

Searches the human-readable text inside files for a keyword or regex pattern, including inside binary files (via printable-string extraction, like `strings`, covering both ASCII and UTF-16LE text) and `.torrent` files (via a built-in bencode parser that reads name/comment/announce-URL fields, including BitTorrent v2 file trees, while skipping binary hash fields).

- **Requirements:** Python 3.7+, standard library only
- **Platform notes:** Pure Python, no external processes or OS-specific paths. Output that the terminal's encoding cannot represent is replaced rather than crashing.
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
- **Exit status:** `0` if anything matched, `1` if nothing matched, `2` on usage errors, `130` if interrupted.
- Run with no keyword argument and it will prompt for one interactively.
- Only regular files are searched; FIFOs, sockets, and device nodes are skipped.
- Control characters in matched text are shown escaped (`\x1b`) so file contents cannot inject terminal escape sequences.

---

### `video-processor.py`

Interactive video transcoder built around `ffmpeg`/`ffprobe`, with menus for resolution, codec, and quality/size targeting (fixed size cap, constant-quality, or a flat default bitrate). Probes the source first and prints a per-stream breakdown (container, codecs, profiles, resolution, frame rate, pixel format and bit depth, channel layouts, sample rates, bitrates, languages, dispositions); stops ffmpeg and cleans up partial output on `Ctrl+C`, `SIGTERM`, or a failed encode; and shows live progress during encoding.

- **Not actually agnostic yet:** it encodes via `h264_mediacodec` / `hevc_mediacodec` / `av1_mediacodec` / `vp9_mediacodec` / `vp8_mediacodec`, which is `ffmpeg`'s wrapper around **Android's MediaCodec hardware encoder API**. The script's own comments describe workarounds specifically for Termux/Android (e.g. keeping the progress loop single-threaded to avoid Termux/Android Python threading overhead). It will not run as-is on desktop Linux, Windows, or macOS.
- **Planned:** refactor to detect platform and offer other encoders (e.g. libx264/libx265 or platform-native hardware encoders) on non-Android systems, while keeping the Termux/MediaCodec path as-is, since that's still the primary use case.
- **Requirements:** Termux on Android, `ffmpeg` built with MediaCodec support, `ffprobe`
- **Usage:**
  ```bash
  python video-processor.py [input_file] [--dry-run]
  python video-processor.py               # prompts for the input path
  ```
- **Notable behavior:**
  - `--dry-run` prints the constructed `ffmpeg` command (shell-quoted, copy-pasteable) without executing it
  - Warns (but doesn't block) on HDR sources, since MediaCodec encodes to SDR without tone-mapping
  - Decoding is deliberately left on CPU: hardware decode caused memory-sync artifacts and was slower in testing
  - Resolution presets are an upper bound: sources are never upscaled, and portrait sources get the box rotated (a "1080p" portrait video becomes 1080x1920)
  - Audio and subtitle tracks are handled per track for the target container: audio that can't be copied is re-encoded (AAC for MP4, Opus for WebM); text subtitles are converted (`mov_text` for MP4, WebVTT for WebM, SRT for MKV when the source is `mov_text`); bitmap subtitles (PGS/DVD) are dropped for MP4/WebM with a notice
  - Before the size prompt, it shows the source size, the estimated "natural" output size at the source video bitrate, and the audio-only floor, so the size you type can be an informed one
  - Size limits accept `500`, `500MB`, `1.2GB`, `700MiB`, or `50%` of the source size
  - Size-limit mode accounts for every audio track, and says so when the limit is impossible to meet
  - A plan summary before the encode shows input, output, resolution, encoder, rate mode, and the predicted output size, and flags a re-encode to the codec the source already uses
  - The final report gives the output size as a percentage of the source as well as against any limit
  - Constant-quality mode uses MediaCodec's CQ bitrate mode. The quality scale is device-defined and many devices don't support CQ for video at all
