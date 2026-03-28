"""Adapter module for the libpgs CLI tool.

All interaction with the libpgs binary goes through this module.
libpgs streams PGS data as NDJSON lines — one tracks header followed
by display_set lines — which this module converts to the internal
display-set format used throughout ProofPGS.
"""

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

from .constants import ANALYSIS_RESTART_GRACE_S
from .parser import ds_has_content
from .style import error

_DEBUG = os.environ.get("PROOFPGS_DEBUG_ANALYSIS")


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

def _convert_display_set(ds_json: dict) -> dict | None:
    """Convert a libpgs NDJSON display_set to internal structured format.

    Returns a dict with keys: pts, pts_ms, composition, palettes, objects.
    Bitmaps come pre-decoded from libpgs as base64 palette-index buffers.
    """
    # Composition (None if malformed)
    comp_json = ds_json.get("composition")
    if comp_json is not None:
        composition = {
            "video_width": comp_json["video_width"],
            "video_height": comp_json["video_height"],
            "palette_id": comp_json["palette_id"],
            "palette_only": comp_json.get("palette_only", False),
            "objects": comp_json.get("objects", []),
        }
    else:
        composition = None

    # Palettes: flatten all palette entries into {eid: (Y, Cr, Cb, Alpha)}
    palettes = {}
    for pal in ds_json.get("palettes", []):
        for entry in pal.get("entries", []):
            palettes[entry["id"]] = (
                entry["luminance"],
                entry["cr"],
                entry["cb"],
                entry["alpha"],
            )

    # Objects: decode bitmap from structured JSON, keyed by object ID
    objects = {}
    for obj in ds_json.get("objects", []):
        bitmap_b64 = obj.get("bitmap")
        if not bitmap_b64:
            continue
        oid = obj["id"]
        objects[oid] = {
            "width": obj["width"],
            "height": obj["height"],
            "bitmap": base64.b64decode(bitmap_b64),
        }

    return {
        "pts": ds_json.get("pts", 0),
        "pts_ms": ds_json.get("pts_ms", 0.0),
        "composition": composition,
        "palettes": palettes,
        "objects": objects,
    }


# ---------------------------------------------------------------------------
# Track discovery
# ---------------------------------------------------------------------------

def discover_tracks(libpgs_path: str, input_path: str,
                    keep_alive: bool = False):
    """Spawn libpgs and read the tracks header.

    When *keep_alive* is False (default), the process is killed after
    reading the header and a plain list of track dicts is returned.

    When *keep_alive* is True, the process is left running so the
    caller can continue reading display sets from it — avoiding a
    second full read for slow sources (NAS, non-indexed files).
    Returns ``(tracks, proc)``; the caller is responsible for closing
    and killing *proc*.

    Track dicts:
      {track_id, language, container, name, is_default, is_forced,
       display_set_count, indexed}
    """
    def _kill(p):
        try:
            p.stdout.close()
        except Exception:
            pass
        if p.poll() is None:
            p.kill()
        p.wait()

    proc = subprocess.Popen(
        [libpgs_path, "stream", input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        first_line = proc.stdout.readline()
        if not first_line:
            _kill(proc)
            return ([], None) if keep_alive else []
        header = json.loads(first_line)
        if header.get("type") != "tracks":
            _kill(proc)
            return ([], None) if keep_alive else []
        tracks = header.get("tracks", [])
    except Exception:
        _kill(proc)
        return ([], None) if keep_alive else []

    if keep_alive:
        return tracks, proc

    _kill(proc)
    return tracks


# ---------------------------------------------------------------------------
# Single-track streaming
# ---------------------------------------------------------------------------

def stream_file(libpgs_path: str, input_path: str,
                track_id: int = None,
                max_ds: int = None,
                start: str = None,
                end: str = None,
):
    """Stream display sets from libpgs for a single file/track.

    Generator that spawns ``libpgs stream <file> [-t <track_id>]``,
    reads NDJSON lines, and yields each display set in internal format
    as it arrives.  The subprocess is cleaned up when the generator is
    exhausted or closed (e.g. when the consumer stops iterating).

    Args:
        libpgs_path: Path to the libpgs binary.
        input_path:  Path to .sup file or container.
        track_id:    Track ID to extract (None = first/only track).
        max_ds:      Stop after this many *content* display sets
                     (those with object data). None = read all.
        start:       Start timestamp for targeted extraction (None = beginning).
        end:         End timestamp for targeted extraction (None = end of file).

    Yields:
        Display sets in internal format.
    """
    cmd = [libpgs_path, "stream", input_path]
    if track_id is not None:
        cmd += ["-t", str(track_id)]
    if start is not None:
        cmd += ["--start", start]
    if end is not None:
        cmd += ["--end", end]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    content_count = 0

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
            if ds is None:
                continue

            has_content = ds_has_content(ds)
            if has_content:
                content_count += 1


            yield ds

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


# ---------------------------------------------------------------------------
# Multi-track demuxed streaming (batch extraction)
# ---------------------------------------------------------------------------

class QueueIterator:
    """Wraps a Queue as an iterator, yielding items until a None sentinel."""

    def __init__(self, q: queue.Queue):
        self._q = q

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


def stream_file_multi_track(libpgs_path: str, input_path: str,
                            track_ids: list,
                            max_ds: int = None,
                            queue_size: int = 64,
                            start: str = None,
                            end: str = None):
    """Spawn a single libpgs process that streams multiple tracks.

    Returns ``(iterators, reader_thread, proc, mark_done)`` where
    *iterators* is ``{track_id: QueueIterator}`` and *mark_done* is
    a callable ``mark_done(track_id)`` for early consumer termination.

    A background reader thread demuxes NDJSON lines by ``track_id``
    into bounded per-track queues.  Each ``QueueIterator`` yields
    display sets until the stream ends.

    When *max_ds* is set, the reader counts content display sets per
    track and sends the ``None`` sentinel once the limit is reached,
    discarding further data for that track to avoid backpressure
    deadlocks.  Once all tracks are done, the subprocess is killed
    early.

    *mark_done(track_id)* lets a consumer signal early termination
    (e.g. on error).  It marks the track as done and drains its
    queue so the reader thread is never blocked putting to it.

    Args:
        libpgs_path:  Path to the libpgs binary.
        input_path:   Path to container file.
        track_ids:    List of track IDs to stream.
        max_ds:       Per-track content display-set limit (None = no limit).
        queue_size:   Max items per queue before backpressure (default 64).
        start:        Start timestamp for targeted extraction (None = beginning).
        end:          End timestamp for targeted extraction (None = end of file).
    """
    cmd = [libpgs_path, "stream", input_path,
           "-t", ",".join(str(tid) for tid in track_ids)]
    if start is not None:
        cmd += ["--start", start]
    if end is not None:
        cmd += ["--end", end]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    raw_queues = {tid: queue.Queue(maxsize=queue_size) for tid in track_ids}
    iterators = {tid: QueueIterator(q) for tid, q in raw_queues.items()}

    done_tracks = set()
    done_lock = threading.Lock()

    def mark_done(tid):
        """Signal that a consumer is done with *tid* (error / early exit)."""
        with done_lock:
            done_tracks.add(tid)
        # Drain the queue to unblock the reader if it's blocked on put().
        q = raw_queues.get(tid)
        if q is not None:
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    def _reader():
        content_counts = {tid: 0 for tid in track_ids}
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") in ("tracks", None):
                    continue
                if obj.get("type") != "display_set":
                    continue

                tid = obj.get("track_id")
                if tid is None or tid not in raw_queues:
                    continue

                with done_lock:
                    if tid in done_tracks:
                        if len(done_tracks) >= len(track_ids):
                            break
                        continue

                ds = _convert_display_set(obj)
                if ds is None:
                    continue

                if max_ds is not None and ds_has_content(ds):
                    content_counts[tid] += 1
                    if content_counts[tid] >= max_ds:
                        raw_queues[tid].put(ds)
                        raw_queues[tid].put(None)  # sentinel
                        with done_lock:
                            done_tracks.add(tid)
                            if len(done_tracks) >= len(track_ids):
                                break
                        continue

                raw_queues[tid].put(ds)
        except Exception:
            pass
        finally:
            # Signal end-of-stream to any consumers not yet done.
            with done_lock:
                for tid in track_ids:
                    if tid not in done_tracks:
                        raw_queues[tid].put(None)
                        done_tracks.add(tid)
            try:
                proc.stdout.close()
            except Exception:
                pass
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    return iterators, reader, proc, mark_done


# ---------------------------------------------------------------------------
# Multi-track streaming (analysis / all-track extraction)
# ---------------------------------------------------------------------------

def stream_all_tracks(libpgs_path: str, input_path: str,
                      track_ids: list = None,
                      max_ds_per_track: int = None,
                      deadline: float = None,
                      track_check=None,
                      allow_restart: bool = True,
                      existing_proc=None,
                      existing_tracks: list = None,
                      start: str = None,
                      end: str = None) -> tuple:
    """Stream tracks from a single libpgs invocation, demultiplexed.

    Reads NDJSON lines from ``libpgs stream <file> [-t id,...]``
    and groups display sets by ``track_id``.

    When *allow_restart* is True and *track_check* marks a track as
    concluded with other tracks remaining, a short grace period
    (``ANALYSIS_RESTART_GRACE_S``) allows co-located language tracks
    at the same timestamps to also conclude before signalling the
    caller to restart with fewer tracks.  When False (e.g. containers
    without MKV Cues), restarts are suppressed — concluded tracks are
    simply skipped in the loop and streaming continues until all
    tracks finish or the deadline/cap is reached.

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
        allow_restart:    When True (default), break out of the read loop
                          after the grace period so the caller can restart
                          libpgs with fewer tracks (requires MKV Cues).
                          When False, continue streaming in a single pass.
        existing_proc:    A subprocess already streaming NDJSON (from
                          ``discover_tracks(keep_alive=True)``).  Reuses
                          the process to avoid a second full read on
                          slow sources.  The tracks header must already
                          be consumed; pass the parsed tracks list via
                          *existing_tracks*.
        existing_tracks:  Pre-parsed track dicts from the tracks header
                          (required when *existing_proc* is set).

    Returns:
        ``(track_data, concluded_tids)`` where *track_data* is
        ``{track_id: [display_sets]}`` and *concluded_tids* is the set
        of track IDs that were marked concluded by *track_check*.
    """
    if existing_proc is not None:
        proc = existing_proc
        if _DEBUG:
            print(f"  [DEBUG] Reusing existing libpgs process (pid={proc.pid})",
                  flush=True)
    else:
        cmd = [libpgs_path, "stream", input_path]
        if track_ids:
            cmd += ["-t", ",".join(str(tid) for tid in track_ids)]
        if start is not None:
            cmd += ["--start", start]
        if end is not None:
            cmd += ["--end", end]

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

    # Pre-populate from existing_tracks (header already consumed).
    if existing_tracks:
        for t in existing_tracks:
            tid = t["track_id"]
            track_data.setdefault(tid, [])
            content_counts.setdefault(tid, 0)
        if _DEBUG:
            print(f"  [DEBUG] Pre-initialized {len(track_data)} track(s) "
                  f"from existing header: {sorted(track_data.keys())}",
                  flush=True)

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
                if (allow_restart
                        and last_concluded_at is not None
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
            if ds is None:
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
            if (allow_restart
                    and last_concluded_at is not None
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
