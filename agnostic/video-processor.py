#!/usr/bin/env python3
"""
Hardware-accelerated video converter for Termux on Android.
CPU decoding + MediaCodec hardware encoding (Tensor/Snapdragon/etc.)

Usage:
    python video-processor.py [input_file] [--dry-run]
    python video-processor.py               # will prompt for path
"""

import subprocess
import os
import sys
import argparse
import json
import signal
import time


# ---------------------------------------------------------------------------
# Cleanup state (used by signal handler)
# ---------------------------------------------------------------------------

_current_output_file = None


def _cleanup_handler(sig, frame):
    """Remove any partially-written output file on SIGINT / SIGTERM."""
    print("\nInterrupted. Cleaning up...")
    if _current_output_file and os.path.exists(_current_output_file):
        print(f"Removing incomplete output: {_current_output_file}")
        try:
            os.remove(_current_output_file)
        except OSError as e:
            print(f"Warning: could not remove file: {e}")
    print("Exiting.")
    sys.exit(1)


signal.signal(signal.SIGINT, _cleanup_handler)
signal.signal(signal.SIGTERM, _cleanup_handler)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe_video(input_file):
    """Return full ffprobe metadata as a dict (format + streams)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-analyzeduration", "20M",
        "-probesize", "20M",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        input_file,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def get_stream(probe_data, codec_type):
    """Return the first stream of the given type, or None."""
    for s in probe_data.get("streams", []):
        if s.get("codec_type") == codec_type:
            return s
    return None


def detect_hdr(probe_data):
    """
    Return (is_hdr, color_transfer) for the first video stream.
    Detects HDR10 (smpte2084) and HLG (arib-std-b67).
    MediaCodec does not tone-map; encoding HDR source without tone-mapping
    produces washed-out / clipped output.
    """
    video = get_stream(probe_data, "video")
    if not video:
        return False, ""
    ct = video.get("color_transfer", "")
    return ct in {"smpte2084", "arib-std-b67"}, ct


def get_audio_bitrate_kbps(probe_data):
    """
    Best-effort audio bitrate in kbps.
    Priority: audio stream bit_rate -> format bit_rate heuristic -> fallback 128k.
    For 'copy' audio the stream bitrate is the actual cost; for re-encoded
    audio we'll use the chosen target bitrate instead (see calculate_video_bitrate).
    """
    audio = get_stream(probe_data, "audio")
    if audio:
        br = audio.get("bit_rate")
        if br:
            return max(int(br) // 1000, 1)

    # Last resort: assume 128k (conservative)
    return 128


def get_video_bitrate_kbps(probe_data):
    """
    Best-effort video bitrate in kbps.
    Priority: video stream bit_rate -> format-level bit_rate minus audio estimate.
    Returns None if a reliable figure cannot be determined.
    """
    video = get_stream(probe_data, "video")
    if video:
        br = video.get("bit_rate")
        if br:
            return max(int(br) // 1000, 1)

    fmt_br = probe_data.get("format", {}).get("bit_rate")
    if fmt_br:
        total_kbps = int(fmt_br) // 1000
        audio_kbps = get_audio_bitrate_kbps(probe_data)
        estimate   = total_kbps - audio_kbps
        if estimate > 0:
            return estimate

    return None


def estimate_natural_size_mb(source_video_kbps, audio_kbps, duration_sec,
                              container_overhead=0.02):
    """
    Estimate the output file size (MB) if encoded at source_video_kbps.
    Used to decide whether a size cap is necessary at all.
    """
    total_bits = (source_video_kbps + audio_kbps) * 1000 * duration_sec
    return total_bits * (1 + container_overhead) / 8 / 1_000_000


# ---------------------------------------------------------------------------
# Bitrate calculation
# ---------------------------------------------------------------------------

def calculate_video_bitrate(target_mb, duration_sec, audio_kbps, container_overhead=0.02):
    """
    Return the video bitrate in kbps required to hit target_mb.

    Formula:
        available_bits = target_bits * (1 - overhead) - audio_bits
        video_kbps     = available_bits / duration / 1000

    container_overhead: fraction reserved for muxing metadata (default 2%).
    Clamped to a minimum of 100 kbps to avoid producing unplayable files.
    """
    target_bits    = target_mb * 1_000_000 * 8
    audio_bits     = audio_kbps * 1000 * duration_sec
    available_bits = target_bits * (1 - container_overhead) - audio_bits
    video_kbps     = int(available_bits / duration_sec / 1000)
    return max(video_kbps, 100)


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def build_ffmpeg_cmd(input_file, output_file, scale=None,
                     v_codec="h264_mediacodec", bitrate=None,
                     quality=None, extension=".mp4",
                     audio_bitrate_kbps=128):
    """
    Build the ffmpeg command list.

    Decoding is intentionally left on CPU: hardware (MediaCodec) decoding on
    Tensor/Snapdragon causes memory-sync artefacts and is paradoxically slower
    for transcode workflows due to surface buffer copy overhead.

    -analyzeduration / -probesize are increased to handle files with delayed
    or sparsely interleaved streams (common in remuxed MKV / TS files).

    Streams are mapped explicitly so that multi-track files (multiple audio
    languages, forced subtitles, chapters) are preserved rather than dropped
    by ffmpeg's default stream selection logic.
    """
    cmd = [
        "ffmpeg",
        "-analyzeduration", "20M",
        "-probesize", "20M",
        "-i", input_file,
    ]

    # --- Video filter chain ---
    # 1. Scale (optional), preserving aspect ratio
    # 2. Pad to even dimensions (required by most hardware encoders)
    # 3. Normalise pixel format to yuv420p (mandatory for MediaCodec)
    if scale:
        w, h = scale.split(":")
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad='ceil(iw/16)*16:ceil(ih/16)*16',"
            f"format=yuv420p"
        )
    else:
        vf = "scale='ceil(iw/16)*16:ceil(ih/16)*16',format=yuv420p"

    cmd += ["-vf", vf]

    # --- Explicit stream mapping ---
    # -map 0:v:0        first video track
    # -map 0:a?         all audio tracks; '?' = non-fatal if absent
    # -map 0:s?         all subtitle tracks (skipped for WebM — see below)
    # -map_chapters 0   preserve chapter markers
    cmd += ["-map", "0:v:0", "-map", "0:a?"]
    # WebM only supports WebVTT subtitles. Copying any other codec (mov_text,
    # ASS, PGS, SRT) into a WebM container causes ffmpeg to fail at the header
    # write stage with "Invalid argument". Skip subtitles for WebM entirely.
    if extension != ".webm":
        cmd += ["-map", "0:s?"]
    cmd += ["-map_chapters", "0"]

    # --- Video codec ---
    cmd += ["-c:v", v_codec]

    # --- Bitrate / quality management (mutually exclusive) ---
    if bitrate:
        # Target-size mode: hit a specific kbps ceiling
        cmd += [
            "-b:v",     f"{bitrate}k",
            "-maxrate", f"{int(bitrate * 1.5)}k",
            "-bufsize", f"{bitrate * 2}k",
        ]
    elif quality is not None:
        # Constant-quality mode via MediaCodec's -q:v (0 = best, 51 = worst).
        # Behaviour varies slightly between Tensor and Snapdragon firmware.
        cmd += ["-q:v", str(quality)]
    else:
        cmd += ["-b:v", "4M"]

    # --- Audio ---
    # WebM containers require Opus; everything else copies the source audio
    # track untouched (saves time, preserves quality, avoids re-encode drift).
    if extension == ".webm":
        cmd += ["-c:a", "libopus", "-b:a", f"{audio_bitrate_kbps}k"]
    else:
        cmd += ["-c:a", "copy"]

    # --- Subtitles ---
    # WebM subtitle streams are excluded at the mapping stage above.
    # For all other containers, copy subtitle streams as-is. MP4 only supports
    # mov_text; PGS or ASS in an MP4 will error — the fix (use MKV) is obvious.
    if extension != ".webm":
        cmd += ["-c:s", "copy"]

    # -y: never prompt for overwrite confirmation. The output path is already
    # validated by resolve_output_path() before ffmpeg is called, so this is
    # just a safety net — without it ffmpeg hangs silently when stdout is piped.
    cmd += ["-y"]

    # --- Progress to stdout (parsed by run_with_progress) ---
    cmd += ["-progress", "pipe:1", "-nostats"]

    cmd.append(output_file)
    return cmd


# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------

def run_with_progress(cmd, duration_sec):
    """
    Run ffmpeg, printing a live progress percentage.
    Captures stderr; dumps it only on failure.
    Returns the process return code.

    Design notes:
    - stderr is drained on a background thread so its pipe buffer (64 KB)
      never fills up and causes ffmpeg to block, regardless of -nostats.
    - The terminal write (print + flush) is RATE-LIMITED to at most once per
      0.5 s. Without this, at high encode rates (200+ fps) ffmpeg writes one
      progress block per frame: the flush to the Termux terminal becomes the
      bottleneck, the stdout pipe fills up, and ffmpeg stalls mid-encode.
      Separating "drain the pipe as fast as possible" from "update the display
      occasionally" keeps both sides unblocked.
    """
    import threading

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stderr_chunks = []

    def drain_stderr():
        """Continuously read stderr so its pipe buffer never fills up."""
        for line in proc.stderr:
            stderr_chunks.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    # Read stdout (progress key=value pairs) as fast as possible.
    # Only update the terminal display at most every 0.5 s so the flush
    # never becomes the rate-limiting step.
    last_print = 0.0
    last_pct   = 0.0
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                ms = int(line.split("=", 1)[1])
                elapsed = ms / 1_000_000          # microseconds -> seconds
                last_pct = min(elapsed / duration_sec * 100, 100.0)
                now = time.monotonic()
                if now - last_print >= 0.5:
                    print(f"\r  Progress: {last_pct:5.1f}%", end="", flush=True)
                    last_print = now
            except (ValueError, ZeroDivisionError):
                pass

    proc.wait()
    stderr_thread.join()
    # Print the final percentage so the display always ends at 100 %.
    print(f"\r  Progress: {last_pct:5.1f}%")

    if proc.returncode != 0:
        print("\n--- ffmpeg stderr ---")
        print("".join(stderr_chunks).strip())

    return proc.returncode


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_elapsed(seconds):
    """Format an integer number of seconds as HHh MMm SSs."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}h {m:02d}m {s:02d}s"


# ---------------------------------------------------------------------------
# Output path resolution
# ---------------------------------------------------------------------------

def resolve_output_path(proposed_path):
    """
    If proposed_path does not exist, return it unchanged.

    If it does exist, prompt the user:
      o  — overwrite it (ffmpeg's -y handles the actual clobber)
      n  — new name: shows an auto-suggestion; press Enter to accept it or
           type any other filename to use that instead
      a  — abort

    Returns the resolved path string, or None if the user chose to abort.
    """
    if not os.path.exists(proposed_path):
        return proposed_path

    directory = os.path.dirname(proposed_path) or "."
    base, ext = os.path.splitext(proposed_path)

    # Find the first available auto-name (_1, _2, ...) up front so we can
    # show it as the suggestion before the user has to type anything.
    n = 1
    while True:
        auto_candidate = f"{base}_{n}{ext}"
        if not os.path.exists(auto_candidate):
            break
        n += 1

    print(f"\n  Output file already exists: {proposed_path}")
    print(f"    o  Overwrite it")
    print(f"    n  New name  (suggestion: {os.path.basename(auto_candidate)})")
    print(f"    a  Abort")

    while True:
        choice = input("  Choice (o/n/a): ").strip().lower()

        if choice == "o":
            return proposed_path

        elif choice == "n":
            typed = input(
                f"  New filename (Enter to use '{os.path.basename(auto_candidate)}'): "
            ).strip()

            if not typed:
                print(f"  Using: {auto_candidate}")
                return auto_candidate

            # Strip any accidental path separators — keep output in the same directory.
            typed_name = os.path.basename(typed)

            # Append the correct extension if the user omitted it.
            if not typed_name.lower().endswith(ext.lower()):
                typed_name += ext

            final = os.path.join(directory, typed_name)

            if os.path.exists(final):
                print(f"  '{typed_name}' also exists. Please choose a different name.")
                continue  # back to the name prompt, not the top-level menu

            print(f"  Using: {final}")
            return final

        elif choice == "a":
            return None

        else:
            print("  Please enter o, n, or a.")


# ---------------------------------------------------------------------------
# Interactive menus
# ---------------------------------------------------------------------------

def ask_resolution():
    print("\n--- Select Resolution ---")
    options = {
        "1": (None,          "Original"),
        "2": ("3840:2160",   "4K (3840x2160)"),
        "3": ("1920:1080",   "1080p (1920x1080)"),
        "4": ("1280:720",    "720p (1280x720)"),
        "5": ("854:480",     "480p (854x480)"),
    }
    for k, (_, label) in options.items():
        print(f"  {k}. {label}")
    choice = input("Choice (1-5): ").strip()
    scale, label = options.get(choice, (None, "Original"))
    return scale


def ask_codec():
    print("\n--- Select Hardware Encoder ---")
    codec_map = {
        "1": ("h264_mediacodec", ".mp4",  "H.264 (AVC)"),
        "2": ("hevc_mediacodec", ".mp4",  "H.265 (HEVC)"),
        "3": ("av1_mediacodec",  ".mkv",  "AV1"),
        "4": ("vp9_mediacodec",  ".webm", "VP9"),
        "5": ("vp8_mediacodec",  ".webm", "VP8"),
    }
    for k, (_, ext, label) in codec_map.items():
        print(f"  {k}. {label} -> {ext}")
    choice = input("Choice (1-5): ").strip()
    codec, ext, _ = codec_map.get(choice, ("h264_mediacodec", ".mp4", "H.264"))
    return codec, ext


def ask_quality_mode(source_mb, duration_sec, extension,
                     source_audio_kbps, source_video_kbps):
    """
    Ask the user how they want to control output size / quality.

    Three options:
      1. Size limit (MB)     -- caps bitrate only as much as needed to stay
                                under the limit; never inflates beyond source quality
      2. Constant quality    -- let the encoder decide the bitrate via -q:v
      3. Default 4 Mbps      -- fixed 4 Mbps fallback

    A bitrate cap is ALWAYS applied in mode 1 to prevent the unconstrained
    4 Mbps default from blowing past the limit when the source is low-bitrate.

    Returns (bitrate_kbps_or_None, quality_int_or_None, limit_mb_or_None).
    """
    print("\n--- Bitrate / Quality Mode ---")
    print("  1. Size limit (MB)        — caps bitrate only if needed to stay under limit")
    print("  2. Quality value (-q:v)   — constant quality, unpredictable file size")
    print("  3. Default (4 Mbps)       — fixed 4 Mbps, skip both options above")

    mode = input("Choice (1-3, Enter = 3): ").strip() or "3"

    if mode == "1":
        if not (duration_sec and source_mb):
            print("  Size-limit unavailable: probe data missing.")
            return None, None, None

        raw = input(f"  Size limit in MB (source is {source_mb:.1f} MB): ").strip()
        if not raw:
            return None, None, None
        try:
            limit_mb = float(raw)
        except ValueError:
            print("  Invalid number — using default.")
            return None, None, None

        effective_audio_kbps = 128 if extension == ".webm" else source_audio_kbps

        # The hard ceiling: bitrate required to fit inside limit_mb exactly.
        limit_bitrate = calculate_video_bitrate(limit_mb, duration_sec, effective_audio_kbps)

        if source_video_kbps:
            natural_mb = estimate_natural_size_mb(
                source_video_kbps, effective_audio_kbps, duration_sec
            )
            print(f"  Estimated natural output size: ~{natural_mb:.1f} MB")

            if natural_mb <= limit_mb:
                # Natural output fits — cap at source bitrate to avoid inflating
                # the file. Never go unconstrained: the 4 Mbps default can easily
                # exceed the limit when the source bitrate is well below 4 Mbps.
                bitrate = min(source_video_kbps, limit_bitrate)
                print(
                    f"  Natural output fits within {limit_mb:.1f} MB limit. "
                    f"Capping at source bitrate (~{bitrate} kbps) to avoid inflation."
                )
            else:
                # Natural output would exceed the limit — apply the tighter cap.
                bitrate = limit_bitrate
                print(
                    f"  Natural output exceeds limit — capping video bitrate to ~{bitrate} kbps "
                    f"(audio: ~{effective_audio_kbps} kbps, 2% container overhead reserved)"
                )
        else:
            # Can't estimate natural size — apply the limit cap as a safe default.
            bitrate = limit_bitrate
            print(
                f"  Could not estimate natural size; capping to ~{bitrate} kbps "
                f"to stay within {limit_mb:.1f} MB."
            )

        if bitrate < 200:
            print(
                f"  Warning: calculated bitrate is only {bitrate} kbps — "
                f"output quality may be very poor for this size limit."
            )
        return bitrate, None, limit_mb

    elif mode == "2":
        print("  Scale: 0 (best quality) to 51 (worst). Typical range: 18-28.")
        raw = input("  Quality value (Enter = 23): ").strip() or "23"
        try:
            q = max(0, min(51, int(raw)))
        except ValueError:
            print("  Invalid — using default.")
            return None, None, None
        print(f"  Quality: {q}")
        return None, q, None

    else:
        print("  Using default 4 Mbps bitrate.")
        return None, None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hardware-accelerated video converter for Termux/Android.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input", nargs="?",
        help="Path to the input video file (will prompt if omitted).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the ffmpeg command that would run without executing it.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("*** DRY RUN MODE: ffmpeg will not be executed. ***\n")

    # --- Resolve input path ---
    if args.input:
        input_path = args.input.strip()
    else:
        input_path = input("Enter the path to the input video: ").strip()

    if not os.path.exists(input_path):
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    # --- Probe the source file ---
    print("\nProbing source file...")
    try:
        probe      = probe_video(input_path)
        duration   = float(probe["format"]["duration"])
        source_mb  = os.path.getsize(input_path) / 1_000_000
        audio_kbps = get_audio_bitrate_kbps(probe)
        video_kbps = get_video_bitrate_kbps(probe)

        video_stream = get_stream(probe, "video")
        res_str = ""
        if video_stream:
            w = video_stream.get("width", "?")
            h = video_stream.get("height", "?")
            fps_raw = video_stream.get("r_frame_rate", "")
            try:
                num, den = fps_raw.split("/")
                fps = f"{int(num)/int(den):.2f} fps"
            except Exception:
                fps = fps_raw
            res_str = f" | {w}x{h} @ {fps}"

        print(
            f"  Duration: {duration:.1f}s{res_str} | "
            f"Size: {source_mb:.1f} MB | Audio: ~{audio_kbps} kbps"
        )

        # HDR check: warn but do not abort. The user may have a tone-mapped
        # workflow or may simply accept the colour shift.
        is_hdr, color_transfer = detect_hdr(probe)
        if is_hdr:
            print(
                f"\n  WARNING: HDR content detected (color_transfer={color_transfer})."
                f"\n  MediaCodec encodes to SDR without tone-mapping — colours will be"
                f"\n  clipped / washed out in the output. Proceeding anyway."
            )

    except Exception as e:
        print(f"  Warning: probe failed ({e}). Size-limit feature disabled.")
        duration   = None
        source_mb  = None
        audio_kbps = 128
        video_kbps = None

    # --- Menus ---
    selected_scale         = ask_resolution()
    selected_codec, ext    = ask_codec()
    bitrate, quality, target_mb = ask_quality_mode(
        source_mb, duration, ext, audio_kbps, video_kbps
    )

    # --- Output path ---
    base, in_ext = os.path.splitext(input_path)
    if in_ext.lower() == ext.lower():
        proposed = f"{base}_converted{ext}"
    else:
        proposed = f"{base}{ext}"

    output_path = resolve_output_path(proposed)
    if output_path is None:
        print("Aborted.")
        sys.exit(0)

    # --- Build command ---
    cmd = build_ffmpeg_cmd(
        input_path, output_path,
        scale=selected_scale,
        v_codec=selected_codec,
        bitrate=bitrate,
        quality=quality,
        extension=ext,
        audio_bitrate_kbps=128,
    )

    print(f"\nRunning: {' '.join(cmd)}\n")

    # --- Dry run short-circuit ---
    if args.dry_run:
        print("[DRY RUN] No transcode performed.")
        sys.exit(0)

    # --- Register output path so the signal handler can clean it up ---
    global _current_output_file
    _current_output_file = output_path

    # --- Run ---
    time_start = time.monotonic()

    if duration:
        ret = run_with_progress(cmd, duration)
    else:
        ret = subprocess.run(cmd).returncode

    elapsed = int(time.monotonic() - time_start)
    _current_output_file = None   # clear before checking ret so handler won't fire late

    if ret != 0:
        print(f"Conversion failed. Elapsed: {format_elapsed(elapsed)}")
        sys.exit(ret)

    # --- Post-encode report ---
    if os.path.exists(output_path):
        actual_mb = os.path.getsize(output_path) / 1_000_000
        print(f"\nDone!  Elapsed: {format_elapsed(elapsed)}")
        print(f"  Output: {output_path}")
        print(f"  Output size: {actual_mb:.2f} MB", end="")
        if target_mb:
            deviation = (actual_mb - target_mb) / target_mb * 100
            sign = "+" if deviation >= 0 else ""
            over_under = "over limit" if deviation > 0 else "under limit"
            print(f"  (limit: {target_mb:.1f} MB, {sign}{deviation:.1f}% {over_under})", end="")
        print()


if __name__ == "__main__":
    main()
