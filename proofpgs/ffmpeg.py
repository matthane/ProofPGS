"""FFmpeg/ffprobe integration: probing, extraction, and streaming."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

from .constants import format_time, ANALYSIS_MAX_PACKETS
from .parser import read_sup_streaming


def check_ffmpeg():
    """Return (ffmpeg_path, ffprobe_path) or exit if not found."""
    ffmpeg  = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = []
        if not ffmpeg:  missing.append("ffmpeg")
        if not ffprobe: missing.append("ffprobe")
        print(f"[error] {', '.join(missing)} not found on PATH.\n"
              f"        Install ffmpeg (https://ffmpeg.org) to process container files.",
              file=sys.stderr)
        sys.exit(1)
    return ffmpeg, ffprobe


def probe_pgs_tracks(ffprobe_path: str, input_path: str) -> tuple:
    """Use ffprobe to discover all PGS subtitle streams in a container.

    Returns (tracks, duration_s) where tracks is a list of dicts with keys:
      index, language, title, forced, default, num_frames
    and duration_s is the container duration in seconds (or None).
    """
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "s",
             "-show_format", input_path],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[error] ffprobe failed on {input_path}: {e.stderr.strip()}")
        return [], None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"[error] Could not parse ffprobe output for {input_path}.")
        return [], None

    duration_s = None
    try:
        duration_s = float(data.get("format", {}).get("duration", ""))
    except (ValueError, TypeError):
        pass

    tracks = []
    for s in data.get("streams", []):
        if s.get("codec_name") != "hdmv_pgs_subtitle":
            continue
        tags = s.get("tags", {})
        disp = s.get("disposition", {})
        # NUMBER_OF_FRAMES is a container-level tag (MKV stats) — cheap,
        # no full-file scan needed.  Not present in all containers.
        num_frames = None
        for key in ("NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng"):
            if key in tags:
                try:
                    num_frames = int(tags[key])
                except (ValueError, TypeError):
                    pass
                break
        # Fallback: some containers (e.g. MP4) populate nb_frames at the
        # stream level for free — no extra probe needed.
        if num_frames is None:
            nb = s.get("nb_frames")
            if nb and nb != "N/A":
                try:
                    num_frames = int(nb)
                except (ValueError, TypeError):
                    pass
        tracks.append({
            "index":      s["index"],
            "language":   tags.get("language", "und"),
            "title":      tags.get("title", ""),
            "forced":     bool(disp.get("forced", 0)),
            "default":    bool(disp.get("default", 0)),
            "num_frames": num_frames,
        })
    return tracks, duration_s


SAMPLE_SECONDS = 120  # 2-minute sample window for packet counting
# A PGS display set consists of multiple segments (PCS, WDS, PDS, ODS,
# END…).  ffprobe -count_packets counts raw segments, but MKV's
# NUMBER_OF_FRAMES counts at the display-set level.  We normalise
# sampled packet counts to display-set estimates using this divisor.
SEGMENTS_PER_DS = 5


def sample_packet_counts(ffprobe_path: str, input_path: str,
                         stream_indices: list, duration_s: float) -> tuple:
    """Estimate PGS display sets via a 2-min mid-file packet sample.

    Samples from the midpoint to avoid intros/credits that lack subtitles.
    Uses ffprobe -read_intervals to avoid reading the entire container.
    Raw packet counts are divided by SEGMENTS_PER_DS to convert from
    PGS segments to display-set estimates, matching the unit used by
    MKV's NUMBER_OF_FRAMES tag.

    Returns (counts_dict, is_estimated) where counts_dict maps
    stream index -> estimated display-set count, and is_estimated
    is True when the count was extrapolated from a sample.
    Returns ({}, False) on any failure (graceful degradation).
    """
    if not duration_s or duration_s <= 0:
        return {}, False

    is_estimated = duration_s > SAMPLE_SECONDS

    cmd = [ffprobe_path, "-v", "quiet", "-print_format", "json",
           "-count_packets", "-show_streams", "-select_streams", "s"]

    if is_estimated:
        # File is longer than the sample window — read a 2-minute slice
        # from the middle to avoid intros/credits that lack subtitles.
        midpoint = max(0, (duration_s - SAMPLE_SECONDS) / 2)
        cmd += ["-read_intervals", f"{midpoint}%+{SAMPLE_SECONDS}"]

    # Short files: skip -read_intervals and just count the whole file.
    cmd.append(input_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=True)
    except subprocess.CalledProcessError:
        return {}, False

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}, False

    counts = {}
    for s in data.get("streams", []):
        idx = s.get("index")
        if idx not in stream_indices:
            continue
        nb = s.get("nb_read_packets")
        if nb is None:
            continue
        try:
            sample_count = int(nb)
        except (ValueError, TypeError):
            continue
        # Convert raw segment count to display-set estimate, then
        # extrapolate to the full duration if we only sampled a window.
        ds_count = sample_count / SEGMENTS_PER_DS
        if is_estimated:
            counts[idx] = round(ds_count * (duration_s / SAMPLE_SECONDS))
        else:
            counts[idx] = round(ds_count)

    return counts, is_estimated


def extract_all_pgs_tracks(ffmpeg_path: str, input_path: str,
                           tracks: list, temp_dir: str,
                           duration_s: float = None) -> dict:
    """Extract all PGS tracks from a container in a single ffmpeg pass.

    Returns a dict mapping pgs_index -> temp .sup file path.
    Reads the container file only once. Shows progress if duration is known.
    """
    cmd = [ffmpeg_path, "-v", "error", "-progress", "pipe:1",
           "-i", input_path]
    sup_paths = {}
    for ti, track in enumerate(tracks):
        sup_path = os.path.join(temp_dir, f"track_{ti}.sup")
        cmd += ["-map", f"0:{track['index']}", "-c", "copy", sup_path]
        sup_paths[ti] = sup_path

    print(f"Extracting {len(tracks)} PGS track(s)...")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if not duration_s:
                continue
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    time_us = int(line.split("=", 1)[1])
                    if time_us >= 0:
                        pos_s = time_us / 1_000_000
                        pct = min(100.0, pos_s / duration_s * 100)
                        print(f"\r  Progress: {pct:5.1f}%  "
                              f"({format_time(pos_s)} / "
                              f"{format_time(duration_s)})",
                              end="", flush=True)
                except (ValueError, ZeroDivisionError):
                    pass
    finally:
        if proc.poll() is None:
            proc.kill()
        rc = proc.wait()

    if duration_s:
        print(f"\r  Progress: 100.0%  "
              f"({format_time(duration_s)} / {format_time(duration_s)})")
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)

    return sup_paths


def extract_analysis_samples(ffmpeg_path: str, input_path: str,
                              tracks: list, temp_dir: str,
                              seek_s: float = None,
                              max_packets: int = ANALYSIS_MAX_PACKETS,
                              deadline: float = None) -> list:
    """Single FFmpeg pass to extract PGS samples for all tracks at once.

    Writes each track to a separate temp .sup file.  Uses ``-ss`` to seek
    to the middle of the file and ``-frames:s`` to cap each output at
    *max_packets* packets (~25 display sets).

    Two exit conditions (whichever fires first):
      1. ``-frames:s`` cap — FFmpeg exits when ALL outputs are capped.
      2. Watchdog *deadline* — kills FFmpeg at the wallclock budget limit.

    Displays ``Scanning... (Xs elapsed)`` via FFmpeg's ``-progress``
    output, cleared when extraction finishes.

    Returns a list of temp ``.sup`` file paths (index-aligned with
    *tracks*).

    stderr is sent to DEVNULL to prevent the Windows 4 KB pipe-buffer
    deadlock (same rationale as ``extract_track_streaming``).
    """
    cmd = [ffmpeg_path, "-v", "error"]
    if seek_s is not None and seek_s > 0:
        cmd += ["-ss", str(seek_s)]

    cmd += ["-progress", "pipe:1"]

    cmd += ["-i", input_path]

    sup_paths = []
    for i, track in enumerate(tracks):
        sup_path = os.path.join(temp_dir, f"track_{i}.sup")
        # -flush_packets 1: flush after every packet so data is on disk
        # if the watchdog kills FFmpeg before it exits naturally.
        cmd += ["-map", f"0:{track['index']}", "-c", "copy",
                "-flush_packets", "1"]
        if max_packets is not None:
            cmd += ["-frames:s", str(max_packets)]
        cmd.append(sup_path)
        sup_paths.append(sup_path)

    # --- Launch FFmpeg ---------------------------------------------------
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    # Watchdog thread: kill FFmpeg when the wallclock budget expires.
    cancel = threading.Event()
    if deadline is not None:
        def _watchdog():
            wait = max(0, deadline - time.monotonic())
            if not cancel.wait(timeout=wait):
                try:
                    proc.kill()
                except OSError:
                    pass
                cancel.set()
        threading.Thread(target=_watchdog, daemon=True).start()

    # --- Wait / read progress --------------------------------------------
    try:
        if proc.stdout:
            start_t = time.monotonic()
            print("\r  Scanning...", end="", flush=True)
            for line in proc.stdout:
                if cancel.is_set():
                    break
                if line.strip() == "progress=continue":
                    elapsed = time.monotonic() - start_t
                    print(
                        f"\r  Scanning... "
                        f"({elapsed:.1f}s elapsed)",
                        end="", flush=True,
                    )
            print("\r\033[K", end="", flush=True)  # clear progress line
        proc.wait()
    finally:
        cancel.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    return sup_paths


def build_track_folder_name(pgs_index: int, track_info: dict) -> str:
    """Build a subfolder name like 'track_0_ger' or 'track_2_eng_forced'."""
    parts = [f"track_{pgs_index}", track_info["language"]]
    if track_info["forced"]:
        parts.append("forced")
    if track_info["default"]:
        parts.append("default")
    return "_".join(parts)


def extract_track_streaming(ffmpeg_path: str, input_path: str,
                            stream_index: int, max_ds: int = None,
                            seek_s: float = None,
                            read_duration_s: float = None) -> list:
    """Extract a single PGS track via pipe, parsing incrementally.

    Terminates FFmpeg early once max_ds display sets are collected,
    so only the portion of the container up to the last needed subtitle
    is read from disk.  For a 50 GB movie where the first 10 subtitles
    appear in the first 5 minutes, this reads only ~5 minutes of data.

    Optional seek_s / read_duration_s allow targeting a specific window
    (e.g. the middle of the file for preview sampling).

    No temp files are created — everything flows through the pipe.

    stderr is sent to /dev/null to prevent a deadlock: on Windows the
    pipe buffer is only 4 KB, so if FFmpeg writes enough warnings to
    fill it, it blocks on stderr — which also stalls stdout, while our
    code is blocked reading stdout.  Classic subprocess deadlock.

    Returns a list of display sets.
    """
    cmd = [ffmpeg_path, "-v", "error"]
    if seek_s is not None and seek_s > 0:
        cmd += ["-ss", str(seek_s)]
    if read_duration_s is not None and read_duration_s > 0:
        cmd += ["-t", str(read_duration_s)]
    cmd += ["-i", input_path,
            "-map", f"0:{stream_index}",
            "-c", "copy", "-f", "sup", "pipe:1"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    display_sets = []

    try:
        display_sets = read_sup_streaming(proc.stdout, max_ds)
    finally:
        # Close stdout so FFmpeg gets broken-pipe if still writing
        try:
            proc.stdout.close()
        except Exception:
            pass
        # Terminate FFmpeg if still running (early exit)
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    return display_sets
