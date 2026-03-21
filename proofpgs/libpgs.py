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
import threading
import time

from .constants import format_time, ANALYSIS_RESTART_GRACE_S
from .parser import ds_has_content
from .style import error

_DEBUG = os.environ.get("PROOFPGS_DEBUG_ANALYSIS")


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
                      track_ids: list = None,
                      max_ds_per_track: int = None,
                      deadline: float = None,
                      track_check=None) -> tuple:
    """Stream tracks from a single libpgs invocation, demultiplexed.

    Reads NDJSON lines from ``libpgs stream <file> [-t id,...]``
    and groups display sets by ``track_id``.

    When *track_check* marks a track as concluded and other tracks
    remain, a short grace period (``ANALYSIS_RESTART_GRACE_S``)
    allows co-located language tracks at the same timestamps to also
    conclude before signalling the caller to restart with fewer tracks.

    Args:
        libpgs_path:      Path to the libpgs binary.
        input_path:       Path to container file.
        track_ids:        List of track IDs to stream.  Builds
                          ``-t id1,id2,...``.  ``None`` = all tracks.
        max_ds_per_track: Stop collecting for a track after this many
                          *content* display sets. None = no limit.
        deadline:         Monotonic timestamp for early termination.
                          None = no deadline.
        track_check:      Callback ``fn(track_id, display_sets) -> bool``.
                          Called after each new *content* display set for
                          that track.  Return True to mark the track as
                          concluded (no more data collected for it).

    Returns:
        ``(track_data, concluded_tids)`` where *track_data* is
        ``{track_id: [display_sets]}`` and *concluded_tids* is the set
        of track IDs that were marked concluded by *track_check*.
    """
    cmd = [libpgs_path, "stream", input_path]
    if track_ids:
        cmd += ["-t", ",".join(str(tid) for tid in track_ids)]

    if _DEBUG:
        print(f"  [DEBUG] libpgs cmd: {' '.join(cmd)}", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    # Deadline watchdog: kill the process if we're blocked on I/O past
    # the deadline.  Without this, slow sources (NAS) can stall the
    # budget indefinitely because the deadline check inside the read
    # loop never fires.
    _watchdog_cancel = threading.Event()
    if deadline is not None:
        wait_secs = max(0, deadline - time.monotonic())
        def _watchdog():
            if not _watchdog_cancel.wait(wait_secs):
                if proc.poll() is None:
                    if _DEBUG:
                        print(f"\n  [DEBUG] Watchdog killing libpgs "
                              f"(deadline reached)", flush=True)
                    proc.kill()
        _wd_thread = threading.Thread(target=_watchdog, daemon=True)
        _wd_thread.start()

    track_data = {}         # track_id -> list of display sets
    content_counts = {}     # track_id -> int (content DS count)
    completed_tracks = set()  # tracks done (concluded or hit cap)
    concluded_tids = set()  # tracks concluded by track_check
    last_check = 0.0
    last_concluded_at = None  # monotonic time of last track_check conclusion

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
                if _DEBUG:
                    print(f"  [DEBUG] tracks header reports "
                          f"{len(track_data)} track(s): "
                          f"{sorted(track_data.keys())}", flush=True)
                continue

            if obj.get("type") != "display_set":
                continue

            tid = obj.get("track_id")
            if tid is None:
                continue

            # Skip tracks that are already done
            if tid in completed_tracks:
                if len(completed_tracks) >= len(track_data) and len(track_data) > 0:
                    break
                # Grace-period restart: concluded tracks exist but
                # unconcluded tracks remain — check if we should
                # restart with fewer tracks.
                if (last_concluded_at is not None
                        and time.monotonic() - last_concluded_at
                            >= ANALYSIS_RESTART_GRACE_S):
                    if _DEBUG:
                        print(f"\n  [DEBUG] Grace period expired "
                              f"(on skip), "
                              f"{len(completed_tracks)}/{len(track_data)}"
                              f" tracks done — restarting", flush=True)
                    break
                continue

            ds = _convert_display_set(obj)
            if not ds:
                continue

            track_data.setdefault(tid, []).append(ds)
            content_counts.setdefault(tid, 0)

            if ds_has_content(ds):
                content_counts[tid] += 1

                # Per-track detection check
                if track_check is not None and track_check(tid, track_data[tid]):
                    completed_tracks.add(tid)
                    concluded_tids.add(tid)
                    last_concluded_at = time.monotonic()
                    if len(completed_tracks) >= len(track_data) and len(track_data) > 0:
                        break

                # Safety cap
                if max_ds_per_track is not None and content_counts[tid] >= max_ds_per_track:
                    completed_tracks.add(tid)
                    if len(completed_tracks) >= len(track_data) and len(track_data) > 0:
                        break

            # Periodic checks (at most once per second)
            now = time.monotonic()
            if now - last_check >= 1.0:
                last_check = now
                if deadline is not None and now >= deadline:
                    if _DEBUG:
                        print(f"\n  [DEBUG] Deadline reached, breaking",
                              flush=True)
                    break

            # Grace-period restart: if tracks have concluded and the
            # grace period has elapsed, break so the caller can restart
            # libpgs with only the remaining tracks.
            if (last_concluded_at is not None
                    and len(completed_tracks) < len(track_data)
                    and time.monotonic() - last_concluded_at >= ANALYSIS_RESTART_GRACE_S):
                if _DEBUG:
                    print(f"\n  [DEBUG] Grace period expired, "
                          f"{len(completed_tracks)}/{len(track_data)} "
                          f"tracks done — restarting", flush=True)
                break

    finally:
        _watchdog_cancel.set()
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    return track_data, concluded_tids
