"""Adapter module for the libpgs CLI tool.

All interaction with the libpgs binary goes through this module.
libpgs streams PGS data as NDJSON lines — one tracks header followed
by display_set lines — which this module converts to the internal
display-set format used throughout ProofPGS.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time

from .constants import format_time
from .parser import ds_has_content
from .style import error


# ---------------------------------------------------------------------------
# Segment type name -> internal type code mapping
# ---------------------------------------------------------------------------

_SEG_TYPE_MAP = {
    "PresentationComposition": 0x16,
    "WindowDefinition":        0x17,
    "PaletteDefinition":       0x14,
    "ObjectDefinition":        0x15,
    "EndOfDisplaySet":         0x80,
}


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

def check_libpgs() -> str:
    """Find the libpgs binary.

    Search order:
      1. proofpgs/bin/libpgs(.exe) — bundled with the project
      2. shutil.which("libpgs") on PATH

    Returns the absolute path to the binary, or exits with an error.
    """
    bin_dir = os.path.join(os.path.dirname(__file__), "bin")
    exe_name = "libpgs.exe" if sys.platform == "win32" else "libpgs"
    bundled = os.path.join(bin_dir, exe_name)
    if os.path.isfile(bundled):
        return bundled

    on_path = shutil.which("libpgs")
    if on_path:
        return on_path

    print(f"{error('[error]')} libpgs not found.\n"
          f"        Place the binary in proofpgs/bin/ or add it to PATH.\n"
          f"        https://github.com/matthane/libpgs",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# NDJSON helpers
# ---------------------------------------------------------------------------

def _convert_display_set(ds_json: dict) -> list:
    """Convert a libpgs NDJSON display_set object to internal format.

    Internal format: list of {type: int, pts: int, payload: bytes} dicts.
    """
    segments = []
    for seg in ds_json.get("segments", []):
        seg_type = _SEG_TYPE_MAP.get(seg["type"])
        if seg_type is None:
            continue
        payload_b64 = seg.get("payload", "")
        payload = base64.b64decode(payload_b64) if payload_b64 else b""
        segments.append({
            "type": seg_type,
            "pts": seg["pts"],
            "payload": payload,
        })
    return segments


# ---------------------------------------------------------------------------
# Track discovery
# ---------------------------------------------------------------------------

def discover_tracks(libpgs_path: str, input_path: str) -> list:
    """Spawn libpgs, read only the tracks header, then kill the process.

    Returns a list of track dicts:
      {track_id, language, container, name, flag_default, flag_forced,
       display_set_count}
    """
    proc = subprocess.Popen(
        [libpgs_path, "stream", input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        first_line = proc.stdout.readline()
        if not first_line:
            return []
        header = json.loads(first_line)
        if header.get("type") != "tracks":
            return []
        return header.get("tracks", [])
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Single-track streaming
# ---------------------------------------------------------------------------

def stream_file(libpgs_path: str, input_path: str,
                track_id: int = None,
                max_ds: int = None,
                show_progress: bool = False) -> list:
    """Stream display sets from libpgs for a single file/track.

    Spawns ``libpgs stream <file> [-t <track_id>]``, reads NDJSON lines,
    and converts each display_set into internal format.

    Args:
        libpgs_path: Path to the libpgs binary.
        input_path:  Path to .sup file or container.
        track_id:    Track ID to extract (None = first/only track).
        max_ds:      Stop after this many *content* display sets
                     (those with ODS segments). None = read all.
        show_progress: Show streaming progress line.

    Returns:
        List of display sets in internal format.
    """
    cmd = [libpgs_path, "stream", input_path]
    if track_id is not None:
        cmd += ["-t", str(track_id)]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    display_sets = []
    content_count = 0
    showed_progress = False

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            obj = json.loads(line)

            # Skip the tracks header line
            if obj.get("type") == "tracks":
                continue

            if obj.get("type") != "display_set":
                continue

            ds = _convert_display_set(obj)
            if not ds:
                continue

            display_sets.append(ds)
            has_content = ds_has_content(ds)
            if has_content:
                content_count += 1

            if show_progress:
                pts = obj.get("pts", 0)
                pos_s = pts / 90_000.0
                pos_str = format_time(pos_s)
                if max_ds is not None:
                    print(f"\r  Streaming: {content_count}/{max_ds} subtitles "
                          f"(at {pos_str} in file)   ",
                          end="", flush=True)
                    showed_progress = True
                else:
                    if content_count > 0 and content_count % 50 == 0:
                        print(f"\r  Streaming: {content_count} subtitles "
                              f"(at {pos_str} in file)   ",
                              end="", flush=True)
                        showed_progress = True

            if max_ds is not None and content_count >= max_ds:
                break

    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    if showed_progress:
        print()  # newline after progress
    return display_sets


# ---------------------------------------------------------------------------
# Multi-track streaming (analysis / all-track extraction)
# ---------------------------------------------------------------------------

def stream_all_tracks(libpgs_path: str, input_path: str,
                      max_ds_per_track: int = None,
                      deadline: float = None,
                      ready_check=None) -> dict:
    """Stream all tracks from a single libpgs invocation, demultiplexed.

    Reads NDJSON lines from ``libpgs stream <file>`` (no ``-t`` flag)
    and groups display sets by ``track_id``.

    Args:
        libpgs_path:      Path to the libpgs binary.
        input_path:       Path to container file.
        max_ds_per_track: Stop collecting for a track after this many
                          *content* display sets. None = no limit.
        deadline:         Monotonic timestamp for early termination.
                          None = no deadline.
        ready_check:      Callback ``fn(track_data: dict) -> bool``.
                          Called after each display set; if it returns
                          True, streaming stops. ``track_data`` is the
                          partial result dict ``{track_id: [ds, ...]}``.

    Returns:
        ``{track_id: [display_sets]}`` where each display set is in
        internal format.
    """
    cmd = [libpgs_path, "stream", input_path]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    track_data = {}         # track_id -> list of display sets
    content_counts = {}     # track_id -> int (content DS count)
    completed_tracks = set()  # tracks that have reached max_ds
    last_check = 0.0

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            obj = json.loads(line)

            if obj.get("type") == "tracks":
                # Initialize track_data for all known tracks
                for t in obj.get("tracks", []):
                    tid = t["track_id"]
                    track_data.setdefault(tid, [])
                    content_counts.setdefault(tid, 0)
                continue

            if obj.get("type") != "display_set":
                continue

            tid = obj.get("track_id")
            if tid is None:
                continue

            # Skip tracks that already reached their limit
            if tid in completed_tracks:
                # If all tracks are done, stop entirely
                if max_ds_per_track is not None and len(completed_tracks) >= len(track_data):
                    break
                continue

            ds = _convert_display_set(obj)
            if not ds:
                continue

            track_data.setdefault(tid, []).append(ds)
            content_counts.setdefault(tid, 0)

            if ds_has_content(ds):
                content_counts[tid] += 1
                if max_ds_per_track is not None and content_counts[tid] >= max_ds_per_track:
                    completed_tracks.add(tid)
                    if len(completed_tracks) >= len(track_data) and len(track_data) > 0:
                        break

            # Periodic checks (at most once per second)
            now = time.monotonic()
            if now - last_check >= 1.0:
                last_check = now
                if deadline is not None and now >= deadline:
                    break
                if ready_check is not None and ready_check(track_data):
                    break

    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    return track_data
