#!/usr/bin/env python3
"""
Hardware-accelerated video converter for Termux on Android.
CPU decoding + MediaCodec hardware encoding (Tensor/Snapdragon/etc.)

Usage:
    python video-processor.py [input_file] [--dry-run]
    python video-processor.py               # will prompt for path
"""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time


# ---------------------------------------------------------------------------
# Cleanup state (used by signal handler)
# ---------------------------------------------------------------------------

_current_output_file = None
_current_proc = None


def _remove_partial_output(path):
    if path and os.path.exists(path):
        print(f"Removing incomplete output: {path}")
        try:
            os.remove(path)
        except OSError as e:
            print(f"Warning: could not remove file: {e}")


def _cleanup_handler(sig, frame):
    """Stop ffmpeg and remove any partially-written output on SIGINT / SIGTERM.

    SIGINT from the terminal reaches ffmpeg too (same process group), but a
    SIGTERM sent only to this script does not. Without terminating the child
    explicitly, ffmpeg would keep encoding in the background after we exit.
    """
    print("\nInterrupted. Cleaning up...")
    proc = _current_proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    _remove_partial_output(_current_output_file)
    print("Exiting.")
    sys.exit(130 if sig == signal.SIGINT else 143)


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
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def get_streams(probe_data, codec_type):
    """Return all streams of the given type, in file order.

    Video streams flagged as attached pictures (cover art) are excluded, to
    match ffmpeg's '0:V' specifier used in the mapping below.
    """
    streams = []
    for s in (probe_data or {}).get("streams", []):
        if s.get("codec_type") != codec_type:
            continue
        if codec_type == "video" and s.get("disposition", {}).get("attached_pic"):
            continue
        streams.append(s)
    return streams


def get_stream(probe_data, codec_type):
    """Return the first stream of the given type, or None."""
    streams = get_streams(probe_data, codec_type)
    return streams[0] if streams else None


def stream_bitrate_kbps(stream):
    """Bitrate of one stream in kbps, or None if unknown.

    MKV files usually lack a per-stream bit_rate, but mkvmerge writes a
    'BPS' statistics tag that carries the same information.
    """
    candidates = [stream.get("bit_rate")]
    tags = stream.get("tags", {})
    candidates += [tags.get("BPS"), tags.get("BPS-eng")]
    for value in candidates:
        try:
            if value:
                return max(int(value) // 1000, 1)
        except (TypeError, ValueError):
            continue
    return None


def display_dimensions(video_stream):
    """Return (width, height) as displayed, accounting for rotation metadata.

    Phone recordings are commonly stored landscape with a 90/270 degree
    rotation flag. ffmpeg autorotates before our filters run, so the filter
    chain sees the displayed orientation.
    """
    if not video_stream:
        return None, None
    w, h = video_stream.get("width"), video_stream.get("height")
    rotation = None
    for sd in video_stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = sd["rotation"]
            break
    if rotation is None:
        rotation = video_stream.get("tags", {}).get("rotate")
    try:
        if rotation is not None and abs(int(float(rotation))) % 180 == 90:
            w, h = h, w
    except (TypeError, ValueError):
        pass
    return w, h


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


def get_video_bitrate_kbps(probe_data):
    """
    Best-effort video bitrate in kbps.
    Priority: video stream bitrate -> format-level bit_rate minus all audio.
    Returns None if a reliable figure cannot be determined.
    """
    video = get_stream(probe_data, "video")
    if video:
        br = stream_bitrate_kbps(video)
        if br:
            return br

    fmt_br = (probe_data or {}).get("format", {}).get("bit_rate")
    if fmt_br:
        try:
            total_kbps = int(fmt_br) // 1000
        except ValueError:
            return None
        audio_kbps = sum(stream_bitrate_kbps(a) or 128
                         for a in get_streams(probe_data, "audio"))
        estimate = total_kbps - audio_kbps
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

MIN_VIDEO_KBPS = 100


def calculate_video_bitrate(target_mb, duration_sec, audio_kbps, container_overhead=0.02):
    """
    Return the video bitrate in kbps required to hit target_mb.

    Formula:
        available_bits = target_bits * (1 - overhead) - audio_bits
        video_kbps     = available_bits / duration / 1000

    container_overhead: fraction reserved for muxing metadata (default 2%).
    The result is NOT clamped; it can be zero or negative when the audio
    alone exceeds the target. The caller decides what to do about that.
    """
    target_bits = target_mb * 1_000_000 * 8
    audio_bits = audio_kbps * 1000 * duration_sec
    available_bits = target_bits * (1 - container_overhead) - audio_bits
    return int(available_bits / duration_sec / 1000)


# ---------------------------------------------------------------------------
# Stream planning (what to map, and what to copy vs convert per container)
# ---------------------------------------------------------------------------

# Audio codecs that can be stream-copied into each container. Anything else
# is re-encoded. MKV accepts effectively everything, so it is not listed.
_MP4_AUDIO_COPY_OK = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}
_WEBM_AUDIO_COPY_OK = {"opus", "vorbis"}

# Text-based subtitle codecs that can be converted between formats.
# Bitmap subtitles (PGS, DVD, DVB) cannot be converted to text.
_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


def _reencode_audio_kbps(stream):
    channels = stream.get("channels") or 2
    return 128 if channels <= 2 else 256


def plan_streams(probe_data, extension):
    """
    Decide stream mapping and per-stream codecs for the chosen container.

    Returns (args, audio_kbps_total, notes):
      args              ffmpeg -map / -c:a / -c:s arguments
      audio_kbps_total  estimated combined bitrate of ALL output audio tracks,
                        used by the size-limit calculation
      notes             human-readable lines describing conversions/drops

    Output-relative stream indices (-c:a:N, -c:s:N) are used so each track
    can be handled individually.
    """
    args = ["-map", "0:V:0"]   # capital V: skips cover-art "video" streams
    notes = []

    if probe_data is None:
        # No stream info: fall back to a conservative generic mapping.
        args += ["-map", "0:a?"]
        if extension == ".webm":
            args += ["-c:a", "libopus", "-b:a", "128k"]
        else:
            args += ["-c:a", "copy"]
        args += ["-map_chapters", "0"]
        notes.append("No probe data: subtitles skipped, audio copied as-is "
                     "(may fail if the codec does not fit the container).")
        return args, 128, notes

    # --- Audio ---
    audio_total = 0
    for out_idx, stream in enumerate(get_streams(probe_data, "audio")):
        src_idx = stream["index"]
        codec = stream.get("codec_name", "unknown")
        args += ["-map", f"0:{src_idx}"]

        if extension == ".mp4":
            copy_ok = codec in _MP4_AUDIO_COPY_OK
            target_codec = "aac"
        elif extension == ".webm":
            copy_ok = codec in _WEBM_AUDIO_COPY_OK
            target_codec = "libopus"
        else:
            copy_ok = True
            target_codec = None

        if copy_ok:
            args += [f"-c:a:{out_idx}", "copy"]
            audio_total += stream_bitrate_kbps(stream) or 128
        else:
            kbps = _reencode_audio_kbps(stream)
            args += [f"-c:a:{out_idx}", target_codec, f"-b:a:{out_idx}", f"{kbps}k"]
            audio_total += kbps
            notes.append(f"Audio track {out_idx} ({codec}) -> {target_codec} {kbps}k "
                         f"(cannot be copied into {extension})")

    # --- Subtitles ---
    out_idx = 0
    for stream in get_streams(probe_data, "subtitle"):
        src_idx = stream["index"]
        codec = stream.get("codec_name", "unknown")
        is_text = codec in _TEXT_SUB_CODECS

        if extension == ".mkv":
            # MKV cannot hold mov_text (MP4's subtitle format); convert it.
            target = "srt" if codec == "mov_text" else "copy"
        elif extension == ".mp4":
            target = "mov_text" if is_text else None
        elif extension == ".webm":
            target = "webvtt" if is_text else None
        else:
            target = "copy"

        if target is None:
            notes.append(f"Subtitle stream #{src_idx} ({codec}) dropped: "
                         f"bitmap subtitles cannot go into {extension}")
            continue

        args += ["-map", f"0:{src_idx}", f"-c:s:{out_idx}", target]
        if target not in ("copy", codec):
            notes.append(f"Subtitle stream #{src_idx} ({codec}) -> {target}")
        out_idx += 1

    # MKV attachments are usually fonts that ASS subtitles depend on.
    if extension == ".mkv":
        args += ["-map", "0:t?"]

    args += ["-map_chapters", "0"]
    return args, audio_total, notes


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

# ffmpeg's MediaCodec wrapper already pads H.264/HEVC to 16-pixel alignment
# internally and writes crop metadata, so the output keeps its true size.
# It does not do this for the other codecs (it only warns), so those still
# get padded here. The padding shows up as up to 15 px of black edge.
_SELF_ALIGNING_ENCODERS = {"h264_mediacodec", "hevc_mediacodec"}


def build_video_filter(scale, v_codec, portrait=False):
    """
    Build the -vf chain:
      1. Scale (optional): fit inside the chosen box, never upscale, keep
         even dimensions. The box is flipped for portrait sources so a
         "1080p" preset gives 1080x1920 rather than 608x1080.
      2. Pad to 16-px alignment, only for encoders that need it.
      3. Normalise pixel format to yuv420p (mandatory for MediaCodec).
    """
    parts = []
    if scale:
        w, h = scale.split(":")
        if portrait:
            w, h = h, w
        parts.append(
            f"scale=w='min(iw,{w})':h='min(ih,{h})'"
            f":force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    if v_codec not in _SELF_ALIGNING_ENCODERS:
        parts.append("pad='ceil(iw/16)*16':'ceil(ih/16)*16'")
    parts.append("format=yuv420p")
    return ",".join(parts)


def build_ffmpeg_cmd(input_file, output_file, stream_args, scale=None,
                     v_codec="h264_mediacodec", bitrate=None,
                     quality=None, portrait=False):
    """
    Build the ffmpeg command list.

    Decoding is intentionally left on CPU: hardware (MediaCodec) decoding on
    Tensor/Snapdragon causes memory-sync artefacts and is paradoxically slower
    for transcode workflows due to surface buffer copy overhead.

    -analyzeduration / -probesize are increased to handle files with delayed
    or sparsely interleaved streams (common in remuxed MKV / TS files).
    """
    cmd = [
        "ffmpeg", "-hide_banner",
        "-analyzeduration", "20M",
        "-probesize", "20M",
        "-i", input_file,
        "-vf", build_video_filter(scale, v_codec, portrait),
    ]
    cmd += stream_args
    cmd += ["-c:v", v_codec]

    # --- Bitrate / quality management (mutually exclusive) ---
    if bitrate:
        # Target-size mode. Note: ffmpeg's MediaCodec wrapper only passes
        # -b:v to the device; -maxrate/-bufsize are ignored by it. They are
        # kept for software/other hardware encoders.
        cmd += [
            "-b:v", f"{bitrate}k",
            "-maxrate", f"{int(bitrate * 1.5)}k",
            "-bufsize", f"{bitrate * 2}k",
        ]
    elif quality is not None:
        # MediaCodec constant-quality mode. The quality value is only passed
        # to the device when bitrate_mode is cq, and it must be set via
        # -global_quality directly: -q:v multiplies the value by 118
        # (FF_QP2LAMBDA) before the encoder sees it.
        cmd += ["-bitrate_mode", "cq", "-global_quality:v", str(quality)]
    else:
        cmd += ["-b:v", "4M"]

    # -y: resolve_output_path() already asked about overwriting, and without
    # it ffmpeg would block on a prompt nobody can see.
    cmd += ["-y"]

    # --- Progress to stdout (parsed by run_ffmpeg) ---
    cmd += ["-progress", "pipe:1", "-nostats"]

    cmd.append(output_file)
    return cmd


# ---------------------------------------------------------------------------
# Running ffmpeg with progress
# ---------------------------------------------------------------------------

def run_ffmpeg(cmd, duration_sec):
    """
    Run ffmpeg, printing live progress. Returns ffmpeg's exit code.

    stderr goes to a temporary file rather than a pipe. With both stdout and
    stderr piped and only stdout being read, ffmpeg blocks as soon as it
    writes more than one pipe buffer (64 KiB) of log output, which in turn
    stops progress output and hangs this script forever. A file never fills
    up, and this keeps the loop single-threaded.
    """
    global _current_proc

    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=errlog,
            text=True, encoding="utf-8", errors="replace",
        )
        _current_proc = proc
        try:
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key not in ("out_time_us", "out_time_ms"):
                    continue   # both keys are microseconds (ffmpeg quirk)
                try:
                    elapsed = int(value) / 1_000_000
                except ValueError:
                    continue   # "N/A" before the first frame
                if duration_sec:
                    pct = min(elapsed / duration_sec * 100, 100.0)
                    print(f"\r  Progress: {pct:5.1f}%", end="", flush=True)
                else:
                    print(f"\r  Encoded: {format_elapsed(int(elapsed))}", end="", flush=True)
            proc.wait()
        finally:
            _current_proc = None
        print()

        if proc.returncode != 0:
            errlog.seek(0)
            lines = errlog.read().decode("utf-8", errors="replace").strip().splitlines()
            print("\n--- ffmpeg stderr (last 40 lines) ---")
            print("\n".join(lines[-40:]))

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
      o  - overwrite it (ffmpeg's -y handles the actual clobber)
      n  - new name: shows an auto-suggestion; press Enter to accept it or
           type any other filename to use that instead
      a  - abort

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
    print("    o  Overwrite it")
    print(f"    n  New name  (suggestion: {os.path.basename(auto_candidate)})")
    print("    a  Abort")

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

            # Strip any accidental path separators; keep output in the same directory.
            typed_name = os.path.basename(typed)

            # Append the correct extension if the user omitted it.
            if not typed_name.lower().endswith(ext.lower()):
                typed_name += ext

            final = os.path.join(directory, typed_name)

            if os.path.exists(final):
                print(f"  '{typed_name}' also exists. Please choose a different name.")
                continue

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
    print("  (Presets are an upper bound: smaller sources are never upscaled,")
    print("   and portrait sources get the box rotated to match.)")
    choice = input("Choice (1-5, Enter = 1): ").strip() or "1"
    if choice not in options:
        print("  Invalid choice, keeping original resolution.")
    scale, _ = options.get(choice, (None, "Original"))
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
    choice = input("Choice (1-5, Enter = 1): ").strip() or "1"
    if choice not in codec_map:
        print("  Invalid choice, using H.264.")
    codec, ext, _ = codec_map.get(choice, codec_map["1"])
    return codec, ext


def ask_quality_mode(source_mb, duration_sec, audio_kbps, source_video_kbps):
    """
    Ask the user how they want to control output size / quality.

    Three options:
      1. Size limit (MB)     - caps bitrate only as much as needed to stay
                               under the limit; never inflates beyond source
      2. Constant quality    - MediaCodec CQ mode, device-dependent
      3. Default 4 Mbps      - fixed 4 Mbps fallback

    audio_kbps is the combined bitrate of every output audio track.

    Returns (bitrate_kbps_or_None, quality_int_or_None, limit_mb_or_None).
    """
    print("\n--- Bitrate / Quality Mode ---")
    print("  1. Size limit (MB)        - caps bitrate only if needed to stay under limit")
    print("  2. Constant quality (CQ)  - device-dependent, unpredictable file size")
    print("  3. Default (4 Mbps)       - fixed 4 Mbps, skip both options above")

    mode = input("Choice (1-3, Enter = 3): ").strip() or "3"

    if mode == "1":
        if not (duration_sec and source_mb):
            print("  Size-limit unavailable: probe data missing. Using default 4 Mbps.")
            return None, None, None

        raw = input(f"  Size limit in MB (source is {source_mb:.1f} MB): ").strip()
        if not raw:
            return None, None, None
        try:
            limit_mb = float(raw)
            if limit_mb <= 0:
                raise ValueError
        except ValueError:
            print("  Invalid number. Using default 4 Mbps.")
            return None, None, None

        limit_bitrate = calculate_video_bitrate(limit_mb, duration_sec, audio_kbps)

        if limit_bitrate < MIN_VIDEO_KBPS:
            audio_mb = audio_kbps * 1000 * duration_sec / 8 / 1_000_000
            print(
                f"  Warning: {limit_mb:.1f} MB cannot be met. Audio alone is "
                f"~{audio_mb:.1f} MB ({audio_kbps} kbps total). Encoding video at "
                f"the {MIN_VIDEO_KBPS} kbps floor; the output WILL exceed the limit."
            )
            return MIN_VIDEO_KBPS, None, limit_mb

        if source_video_kbps:
            natural_mb = estimate_natural_size_mb(source_video_kbps, audio_kbps, duration_sec)
            print(f"  Estimated natural output size: ~{natural_mb:.1f} MB")

            if natural_mb <= limit_mb:
                # Natural output fits: cap at source bitrate to avoid inflating
                # the file. Never go unconstrained; the 4 Mbps default can
                # easily exceed the limit when the source is below 4 Mbps.
                bitrate = min(source_video_kbps, limit_bitrate)
                print(
                    f"  Natural output fits within {limit_mb:.1f} MB limit. "
                    f"Capping at source bitrate (~{bitrate} kbps) to avoid inflation."
                )
            else:
                bitrate = limit_bitrate
                print(
                    f"  Natural output exceeds limit. Capping video bitrate to ~{bitrate} kbps "
                    f"(audio: ~{audio_kbps} kbps, 2% container overhead reserved)"
                )
        else:
            bitrate = limit_bitrate
            print(
                f"  Could not estimate natural size; capping to ~{bitrate} kbps "
                f"to stay within {limit_mb:.1f} MB."
            )

        if bitrate < 200:
            print(
                f"  Warning: calculated bitrate is only {bitrate} kbps. "
                f"Output quality may be very poor for this size limit."
            )
        print("  Note: hardware encoders treat bitrate as a target, not a hard cap.")
        return bitrate, None, limit_mb

    elif mode == "2":
        print("  The quality scale is defined by the device encoder (Android")
        print("  documents it as higher = better; 0-100 is common). Many devices")
        print("  do not support CQ for video at all. If ffmpeg fails to configure")
        print("  the encoder, use size-limit or default mode instead.")
        raw = input("  Quality value (Enter = 70): ").strip() or "70"
        try:
            q = max(0, int(raw))
        except ValueError:
            print("  Invalid. Using default 4 Mbps.")
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
    global _current_output_file

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
    input_path = os.path.expanduser(input_path)

    if not os.path.isfile(input_path):
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    source_mb = os.path.getsize(input_path) / 1_000_000

    # --- Probe the source file ---
    print("\nProbing source file...")
    probe = None
    duration = None
    video_kbps = None
    portrait = False
    try:
        probe = probe_video(input_path)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"  Warning: probe failed ({e}). Size-limit feature disabled.")

    if probe is not None:
        try:
            duration = float(probe.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            print("  Warning: duration unknown. Size-limit and % progress disabled.")
        video_kbps = get_video_bitrate_kbps(probe)

        video_stream = get_stream(probe, "video")
        res_str = ""
        if video_stream:
            w, h = display_dimensions(video_stream)
            if isinstance(w, int) and isinstance(h, int):
                portrait = h > w
            fps_raw = video_stream.get("r_frame_rate", "")
            try:
                num, den = fps_raw.split("/")
                fps = f"{int(num) / int(den):.2f} fps"
            except (ValueError, ZeroDivisionError):
                fps = fps_raw
            res_str = f" | {w}x{h} @ {fps}"

        dur_str = f"{duration:.1f}s" if duration else "unknown"
        n_audio = len(get_streams(probe, "audio"))
        n_subs = len(get_streams(probe, "subtitle"))
        print(
            f"  Duration: {dur_str}{res_str} | Size: {source_mb:.1f} MB | "
            f"Audio tracks: {n_audio} | Subtitle tracks: {n_subs}"
        )

        # HDR check: warn but do not abort. The user may have a tone-mapped
        # workflow or may simply accept the colour shift.
        is_hdr, color_transfer = detect_hdr(probe)
        if is_hdr:
            print(
                f"\n  WARNING: HDR content detected (color_transfer={color_transfer})."
                f"\n  MediaCodec encodes to SDR without tone-mapping; colours will be"
                f"\n  clipped / washed out in the output. Proceeding anyway."
            )

    # --- Menus ---
    selected_scale = ask_resolution()
    selected_codec, ext = ask_codec()

    stream_args, audio_kbps, notes = plan_streams(probe, ext)
    if notes:
        print("\n--- Stream handling ---")
        for note in notes:
            print(f"  {note}")

    bitrate, quality, target_mb = ask_quality_mode(
        source_mb, duration, audio_kbps, video_kbps
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
        input_path, output_path, stream_args,
        scale=selected_scale,
        v_codec=selected_codec,
        bitrate=bitrate,
        quality=quality,
        portrait=portrait,
    )

    print(f"\nRunning: {shlex.join(cmd)}\n")

    if args.dry_run:
        print("[DRY RUN] No transcode performed.")
        sys.exit(0)

    # --- Run ---
    _current_output_file = output_path
    time_start = time.monotonic()
    try:
        ret = run_ffmpeg(cmd, duration)
    except FileNotFoundError:
        _current_output_file = None
        print("Error: ffmpeg not found in PATH.")
        sys.exit(127)
    elapsed = int(time.monotonic() - time_start)

    if ret != 0:
        _remove_partial_output(output_path)
        _current_output_file = None
        print(f"Conversion failed. Elapsed: {format_elapsed(elapsed)}")
        sys.exit(ret)
    _current_output_file = None

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
