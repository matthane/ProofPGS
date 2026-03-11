"""FFmpeg/ffprobe integration: probing, extraction, and streaming."""

import json
import os
import shutil
import subprocess
import sys

from .constants import format_time
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
        tracks.append({
            "index":      s["index"],
            "language":   tags.get("language", "und"),
            "title":      tags.get("title", ""),
            "forced":     bool(disp.get("forced", 0)),
            "default":    bool(disp.get("default", 0)),
            "num_frames": num_frames,
        })
    return tracks, duration_s


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
        rc = proc.wait()

    if duration_s:
        print(f"\r  Progress: 100.0%  "
              f"({format_time(duration_s)} / {format_time(duration_s)})")
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)

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
                            stream_index: int, max_ds: int = None) -> list:
    """Extract a single PGS track via pipe, parsing incrementally.

    Terminates FFmpeg early once max_ds display sets are collected,
    so only the portion of the container up to the last needed subtitle
    is read from disk.  For a 50 GB movie where the first 10 subtitles
    appear in the first 5 minutes, this reads only ~5 minutes of data.

    No temp files are created — everything flows through the pipe.

    stderr is sent to /dev/null to prevent a deadlock: on Windows the
    pipe buffer is only 4 KB, so if FFmpeg writes enough warnings to
    fill it, it blocks on stderr — which also stalls stdout, while our
    code is blocked reading stdout.  Classic subprocess deadlock.

    Returns a list of display sets.
    """
    cmd = [ffmpeg_path, "-v", "error",
           "-i", input_path,
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
