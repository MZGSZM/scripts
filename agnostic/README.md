# agnostic/

Scripts in this folder are intended to run unmodified on Linux, Windows, and macOS: no OS-specific binaries, paths, or shell calls, just Python 3 (and its standard library unless a script says otherwise).

> **Flag:** `video-processor.py` is listed below because it currently lives in this folder, but it is **not actually platform-agnostic yet** (see its entry for details). Planned: refactor it to run cross-platform while keeping the existing Android/Termux path intact as one branch, since that's the primary use case.

## Scripts

### `adb-auto.py`

Finds the port of an Android device's Wireless Debugging service via mDNS and runs `adb connect` against it automatically, so you don't have to read the randomly assigned port off the Developer options screen each time. Browses for `_adb-tls-connect._tcp.local.` broadcasts for up to 5 seconds, then connects. If the advertised address belongs to the machine running the script (e.g. Termux on the phone itself), it connects via `localhost`; otherwise it connects to the advertised LAN address.

- **Requirements:** Python 3, the `zeroconf` package (`pip install zeroconf`; if it's missing the script prints that hint and exits `1`), and `adb` on `PATH`
- **Platform notes:** Pure Python plus `adb`, so it runs the same on Linux, Windows, macOS, and Termux. Whether an address is "this machine" is decided by trying to bind a UDP socket to it, which only succeeds for addresses assigned to a local interface (no packets are sent). IPv4 addresses are preferred when the device advertises both.
- **Usage:**
  ```bash
  pip install zeroconf
  python adb-auto.py
  ```
- **Exit status:**
  - `0` connected (or already connected)
  - `1` `zeroconf` is not installed
  - `2` nothing found within the scan timeout
  - `3` `adb` not found on `PATH`
  - `4` `adb connect` failed or timed out
  - `130` interrupted
- **Notable behavior:**
  - Takes no arguments or options
  - Wireless Debugging must be enabled and the device connected to Wi-Fi; it also assumes the machine running the script has already been paired with the device, since it only runs `adb connect`
  - Acts on the first service it sees and doesn't distinguish between multiple advertised devices
  - `adb connect` often exits `0` even when the connection fails, so success is judged from its output ("connected to" / "already connected to") as well as its exit code

---

### `cookies2cobalt.py`

Converts a Netscape-format `cookies.txt` export into the `cookies.json` file a self-hosted [cobalt](https://github.com/imputnet/cobalt) API reads, so you don't have to hand-build cookie strings. Built against cobalt API 11.7.1; pulls `instagram`/`twitter`/`youtube` cookies straight from the export, and lets you add the token-only services (`reddit`, `instagram_bearer`, `vimeo_bearer`) by hand, since those are OAuth credentials rather than cookies and your browser never stores them.

- **Requirements:** Python 3.8+, standard library only
- **Platform notes:** Pure Python aside from one `os.getuid()` call, which is guarded with `hasattr` so it degrades gracefully on Windows (just skips the "fix ownership" reminder rather than crashing). The output file is written `0600`, but that permission bit — which is what keeps the credentials inside it from being world-readable — only actually restricts access on POSIX systems.
- **Usage:**
  ```bash
  ./cookies2cobalt.py firefox-cookies.txt -o cookies.json
  ./cookies2cobalt.py firefox-cookies.txt -o -                       # preview on stdout
  ./cookies2cobalt.py main.txt alt1.txt alt2.txt -o cookies.json     # multiple accounts
  ./cookies2cobalt.py firefox-cookies.txt --add 'reddit:client_id=xxx; client_secret=yyy; refresh_token=zzz'
  ```
- **Key options:** `-o/--output` (`-` for stdout), `-m/--merge` (merge into an existing file; `--replace` to overwrite a service's entries instead of appending), `-s/--services` (limit which services get pulled from the cookie file), `--all-cookies` (every instagram/twitter cookie, not just cobalt's documented set), `--include-expired`, `--add SERVICE:STRING` (repeatable, for the token-only services), `--list-services`
- **Notable behavior:**
  - Correctly reads `#HttpOnly_`-prefixed lines; parsers that treat those as comments silently drop `sessionid`/`auth_token`, the two cookies that actually keep you logged in
  - Cookie values containing `; ` are skipped with a warning, since cobalt splits the string on exactly that substring
  - `youtube` takes every cookie for the domain (youtubei.js builds a hash from the full cookie header); `instagram`/`twitter` pull only the documented keys unless `--all-cookies` is passed
  - Supports multiple accounts per service — pass several input files and cobalt picks one at random per request
  - Must not be mounted read-only in Docker: cobalt rewrites refreshed cookie values back into this file roughly every 60 seconds, so read-only means those writes fail and sessions quietly go stale
  - Warns if the process isn't running as uid 1000, since cobalt's Docker image runs as that user and needs matching file ownership to read it
  - cobalt's own example file uses the key `vimeo`; the code actually wants `vimeo_bearer` — the wrong one is silently ignored at load, and the script warns if it ends up in the output

---

### `folder-report.py`

Describes a folder's layout, sizes, dates, and file types without copying any file contents into the report, so an AI model or another person can understand what's in a folder without being handed the files. Writes a Markdown report to read and a JSON version to paste or upload. Files are classified by reading at most their first 64 KB and last 4 KB (text files are read in full only to count lines), and none of those bytes end up in the report. It then flags oddities such as extensions that don't match content, sensitive-looking filenames, and empty files, groups repeated copies, and recognizes timestamp-named snapshot folders, folding identical siblings into one entry in the tree to keep backup-heavy reports readable.

Lives in its own folder with a full README: [`folder-report/`](./folder-report/)

- **Requirements:** Python 3.8+, standard library only
- **Usage:**
  ```bash
  ./folder-report.py /path/to/folder
  ```
  Writes `<folder>_report.md` and `<folder>_report.json` to the current directory.
- **Key options:** `--format md|json|both`, `--max-depth`, `--ignore`, `--hash` (confirm real duplicates), `--no-collapse`, `--anonymize` (with `--save-map`), and the opt-in content extras `--csv-headers`, `--json-keys`, `--outline`
- **Notable behavior:**
  - File contents stay out of the report unless one of the opt-in extras is used, and the report header says which were used
  - `--anonymize` replaces names with placeholders, but sizes, dates, and file kinds remain visible
  - Never share the `.map.json` written by `--save-map`; it translates every placeholder back to the real name

---

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
