"""High-level orchestration: process .sup files and video containers."""

import os
import sys
import threading
import time

from .parser import ds_has_content
from .renderer import process_display_sets
from .detect import detect_from_palettes, format_detection
from .libpgs import (stream_file, discover_tracks, stream_all_tracks,
                     stream_file_multi_track,
                     stream_file_multi_track_progressive)
from .ffmpeg import probe_video_range, build_track_folder_name, check_ffprobe
from .interactive import (
    select_tracks_interactive, select_count_interactive,
    select_count_interactive_sup, confirm_validate_bailed,
)
from .constants import (Budget, LISTING_BUDGET_S, ANALYSIS_MAX_DS,
                        DEFAULT_INTERACTIVE_COUNT)
from .style import (
    info, warn, error, success, heading, dim, bold,
    badge_hdr, badge_sdr, badge_unknown, badge_mismatch,
    CURSOR_UP_CLEAR,
)



def _resolve_auto_mode(detection: dict) -> str:
    """Resolve 'auto' mode using a detection result. Returns resolved mode."""
    if detection["verdict"] is not None:
        return detection["verdict"]
    return "compare"


def _build_track_tags(tracks, selected_indices):
    """Build short per-line tags for each selected track.

    Format is ``index:lang`` (e.g. ``0:de``, ``2:en``), matching the
    ``[index]`` shown in the track listing so the user can cross-reference.
    """
    tags = {}
    for ti in selected_indices:
        tags[ti] = f"{ti}:{tracks[ti]['language']}"
    return tags


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _analyze_tracks(tracks, track_indices, libpgs_path, input_path,
                    preview_cache, budget=None, has_cues=True,
                    reuse_proc=None, reuse_tracks=None):
    """Multi-pass analysis: stream tracks via libpgs, restarting with
    fewer tracks as each one reaches a conclusive detection verdict.

    Each pass streams the remaining unvalidated tracks.  When a track's
    detection becomes conclusive and *has_cues* is True, a short grace
    period allows co-located language tracks at the same timestamps to
    also conclude before restarting libpgs with only the remaining
    tracks — letting it use MKV Cues to skip past already-validated
    data.  When *has_cues* is False, restarts are disabled and all
    tracks are streamed in a single pass.

    When *reuse_proc* is provided (a running libpgs subprocess whose
    tracks header has already been consumed), it is used for the first
    pass — avoiding a redundant full read on slow sources.

    Updates each track dict in-place with:
      detection, analysis_bailed
    Caches extracted display sets in *preview_cache*.
    """
    if not track_indices:
        return

    num_tracks = len(track_indices)

    # Live elapsed-time display on the "Analyzing" line.
    _timer_stop = threading.Event()
    _timer_t0 = time.monotonic()
    _is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def _print_timer_label(num_validated=0):
        label = f"  {info('Analyzing')} {num_tracks} PGS track(s)..."
        if num_validated > 0:
            label += f" {dim(f'({num_validated}/{num_tracks} validated)')}"
        return label

    _timer_label = [_print_timer_label()]  # mutable for thread access

    def _tick():
        while not _timer_stop.wait(0.5):
            if _is_tty:
                e = time.monotonic() - _timer_t0
                print(f"\r{_timer_label[0]} {dim(f'{e:.0f}s')}", end="", flush=True)

    if has_cues:
        print(f"  {dim('Subtitle cues available. Using direct extraction.')}")
    elif budget:
        print(f"  {dim(f'Subtitle cues not available. Using sequential extraction with {budget.limit:.0f}s timeout.')}")
    else:
        print(f"  {dim('Subtitle cues not available. Using sequential extraction.')}")
    _timer_thread = threading.Thread(target=_tick, daemon=True)
    print(_timer_label[0], end="", flush=True)
    _timer_thread.start()

    try:
        remaining_tids = [tracks[ti]["track_id"] for ti in track_indices]
        all_data = {}       # track_id -> accumulated display sets
        concluded = {}      # track_id -> detection result (cached)

        _debug = os.environ.get("PROOFPGS_DEBUG_ANALYSIS")
        _pass_num = 0

        while remaining_tids:
            if budget and budget.exhausted():
                if _debug:
                    print(f"\n  [DEBUG] Budget exhausted, stopping",
                          flush=True)
                break

            _pass_num += 1
            if _debug:
                elapsed = time.monotonic() - _timer_t0
                print(f"\n  [DEBUG] Pass {_pass_num}: streaming "
                      f"{len(remaining_tids)} track(s): "
                      f"{remaining_tids} ({elapsed:.2f}s elapsed)",
                      flush=True)

            def track_check(tid, display_sets):
                if tid in concluded:
                    return True
                det = detect_from_palettes(display_sets)
                if det["verdict"] is not None:
                    concluded[tid] = det
                    _timer_label[0] = _print_timer_label(len(concluded))
                    if _debug:
                        elapsed = time.monotonic() - _timer_t0
                        print(f"\n  [DEBUG] Track {tid} concluded: "
                              f"{det['verdict']} ({elapsed:.2f}s elapsed)",
                              flush=True)
                    return True
                return False

            # On the first pass, reuse the discover_tracks process if
            # provided — avoids re-reading from the start on slow I/O.
            _extra = {}
            if reuse_proc is not None:
                _extra["existing_proc"] = reuse_proc
                _extra["existing_tracks"] = reuse_tracks
                reuse_proc = None   # only for the first pass
                reuse_tracks = None

            track_data, done_tids = stream_all_tracks(
                libpgs_path, input_path,
                track_ids=remaining_tids,
                max_ds_per_track=ANALYSIS_MAX_DS,
                deadline=budget.deadline() if budget else None,
                track_check=track_check,
                allow_restart=has_cues,
                **_extra,
            )

            if _debug:
                elapsed = time.monotonic() - _timer_t0
                ds_counts = {tid: len(ds) for tid, ds in track_data.items()
                             if ds}
                print(f"  [DEBUG] Pass {_pass_num} ended: "
                      f"concluded={done_tids}, "
                      f"ds_counts={ds_counts} ({elapsed:.2f}s elapsed)",
                      flush=True)

            # Merge new data into accumulator.
            for tid, ds in track_data.items():
                all_data.setdefault(tid, []).extend(ds)

            # Remove validated tracks from remaining.
            remaining_tids = [tid for tid in remaining_tids
                              if tid not in concluded]

            # If no tracks concluded in this pass and we have remaining
            # tracks, they likely have no data yet — stop to avoid an
            # infinite loop (they'll be marked as bailed below).
            if not done_tids and remaining_tids:
                if _debug:
                    print(f"  [DEBUG] No tracks concluded in pass "
                          f"{_pass_num}, {len(remaining_tids)} "
                          f"remaining — bailing", flush=True)
                break

        # --- Post-loop: assign detection results and cache data ---
        for ti in track_indices:
            t = tracks[ti]
            tid = t["track_id"]
            ds = all_data.get(tid, [])

            # Cap to ANALYSIS_MAX_DS display sets.
            if len(ds) > ANALYSIS_MAX_DS:
                ds = ds[:ANALYSIS_MAX_DS]

            preview_cache[ti] = ds
            content_count = sum(1 for d in ds if ds_has_content(d))

            # Use cached detection if available, otherwise run fresh.
            if tid in concluded:
                t["detection"] = concluded[tid]
            elif ds:
                t["detection"] = detect_from_palettes(ds)
            else:
                t["detection"] = {
                    "verdict": None, "confidence": "low",
                    "max_y": 0, "max_achromatic_y": None,
                    "max_pq_channel": 0, "num_palettes": 0,
                }

            # --- Bail-out ---
            t["analysis_bailed"] = (
                content_count == 0 and t["detection"]["verdict"] is None
            )

    finally:
        # Always stop the timer — including on KeyboardInterrupt.
        _timer_stop.set()
        try:
            _timer_thread.join(timeout=1)
        except (KeyboardInterrupt, OSError):
            pass
        elapsed = time.monotonic() - _timer_t0
        if _is_tty:
            print(f"\r{_timer_label[0]} {dim(f'{elapsed:.1f}s')}")
        else:
            print(f" {dim(f'{elapsed:.1f}s')}")


def _print_track_listing(tracks, video_range=None):
    """Print the track listing with analysis results.

    *video_range* (``"hdr"``/``"sdr"``/``None``) is the dynamic range of
    the container's video stream.  When a subtitle track's detected range
    differs, a "Dynamic range mismatch" badge is appended.

    Returns True if any tracks were bailed (not analyzed).
    """
    has_bailed = False

    # --- Pass 1: build plain-text columns for width calculation ---
    rows = []  # (index_col, stream_col, detail_col, count_plain,
               #  badge_plain, badge_styled, mismatch_styled)

    for ti, t in enumerate(tracks):
        index_col = f"[{ti}]"
        stream_col = f"stream {t['index']}"

        parts = [t["language"]]
        if t["title"]:
            parts.append(f'"{t["title"]}"')
        flags = []
        if t["forced"]:  flags.append("forced")
        if t["default"]: flags.append("default")
        if flags:
            parts.append(f"[{', '.join(flags)}]")
        detail_col = "  ".join(parts)

        # Approximate subtitle count from display-set metadata.
        # Each visible subtitle typically produces 2 display sets
        # (one to show, one to clear), so num_frames / 2 ≈ subtitle count.
        num_frames = t.get("num_frames")
        if num_frames and num_frames > 0:
            approx_subs = max(1, num_frames // 2)
            count_plain = f"~{approx_subs:,} subs"
        else:
            count_plain = ""

        if t.get("analysis_bailed"):
            has_bailed = True
            badge_plain = "[not analyzed — too few samples] *"
            badge_styled = badge_unknown(badge_plain)
        else:
            det = t.get("detection", {})
            if det.get("verdict") == "hdr":
                badge_plain = "Mastered for HDR"
                badge_styled = badge_hdr(badge_plain)
            elif det.get("verdict") == "sdr":
                badge_plain = "Mastered for SDR"
                badge_styled = badge_sdr(badge_plain)
            elif det.get("verdict"):
                badge_plain = f"Mastered for {det['verdict'].upper()}"
                badge_styled = badge_plain
            else:
                badge_plain = ""
                badge_styled = ""

        # Mismatch badge: subtitle range differs from video stream range.
        mismatch_styled = ""
        det = t.get("detection", {})
        if (video_range is not None
                and not t.get("analysis_bailed")
                and det.get("verdict") is not None
                and det["verdict"] != video_range):
            mismatch_styled = f"  {badge_mismatch('Dynamic range mismatch')}"

        rows.append((index_col, stream_col, detail_col, count_plain,
                     badge_plain, badge_styled, mismatch_styled))

    # --- Compute column widths ---
    idx_w    = max((len(r[0]) for r in rows), default=0)
    stream_w = max((len(r[1]) for r in rows), default=0)
    detail_w = max((len(r[2]) for r in rows), default=0)
    count_w  = max((len(r[3]) for r in rows), default=0)

    # --- Pass 2: print aligned ---
    print(f"{info('Found')} {bold(str(len(tracks)))} PGS subtitle track(s):")
    if video_range is not None:
        range_label = video_range.upper()
        range_styled = badge_hdr(range_label) if video_range == "hdr" else badge_sdr(range_label)
        print(f"  {dim('Video stream:')} {range_styled}")
    for (index_col, stream_col, detail_col, count_plain,
         badge_plain, badge_styled, mismatch_styled) in rows:
        idx_part = bold(index_col.ljust(idx_w))
        stream_part = dim(stream_col.ljust(stream_w))
        has_trailing = badge_plain or count_plain
        if has_trailing:
            detail_part = detail_col.ljust(detail_w)
            if count_w:
                if count_plain:
                    count_part = f"  {dim(count_plain.rjust(count_w))}"
                else:
                    count_part = f"  {' ' * count_w}"
            else:
                count_part = ""
            if badge_plain:
                badge_part = f"  {badge_styled}"
            else:
                badge_part = ""
            print(f"  {idx_part}  {stream_part}  {detail_part}{count_part}{badge_part}{mismatch_styled}")
        else:
            print(f"  {idx_part}  {stream_part}  {detail_col}")

    if has_bailed:
        print()
        print(dim("  * Very sparse tracks with few subtitles may require reading"))
        print(dim("    deep into the file and take longer."))
    print()

    return has_bailed


# ---------------------------------------------------------------------------
# Batch extraction (single-pass, no cues)
# ---------------------------------------------------------------------------

def _batch_extract_no_cues(libpgs_path, input_path, selected_indices,
                           tracks, track_modes, track_tags, tonemap,
                           nocrop, out_dir, threads,
                           start=None, end=None):
    """Extract all selected tracks in a single libpgs pass (no limit).

    A reader thread demuxes the NDJSON stream into per-track queues,
    and one consumer thread per track feeds its queue into
    ``process_display_sets`` for overlapped rendering.  Using a single
    libpgs invocation avoids redundant MKV header / cues parsing and
    (for containers without cues) re-reading the file from the start.

    Returns total images saved across all tracks.
    """
    track_ids = [tracks[ti]["track_id"] for ti in selected_indices]

    try:
        iterators, reader, proc, mark_done = stream_file_multi_track(
            libpgs_path, input_path, track_ids,
            start=start, end=end)
    except Exception as e:
        print(f"  {error('[error]')} Multi-track extraction failed: {e}")
        return 0

    results = {}  # ti -> saved count
    consumer_threads = []

    for ti in selected_indices:
        track = tracks[ti]
        folder_name = build_track_folder_name(ti, track)
        track_out = os.path.join(out_dir, folder_name)
        track_label = f"Stream {track['index']}: {track['language']}"
        if track["title"]:
            track_label += f' "{track["title"]}"'

        q_iter = iterators[track["track_id"]]

        def _consume(ti=ti, q_iter=q_iter, track_out=track_out,
                     track_label=track_label):
            saved = process_display_sets(
                q_iter, track_out, track_modes[ti], tonemap, nocrop,
                limit=None,
                detection=tracks[ti].get("detection"),
                input_name=os.path.basename(input_path),
                track_name=track_label,
                threads=threads,
                track_tag=track_tags[ti],
            )
            results[ti] = saved

        t = threading.Thread(target=_consume)
        t.start()
        consumer_threads.append(t)

    for t in consumer_threads:
        t.join()
    reader.join()

    total = 0
    for ti in selected_indices:
        saved = results.get(ti, 0)
        total += saved
        if saved == 0:
            tag = track_tags[ti]
            print(f"  {dim(tag)}  No subtitles found.")
    return total


def _batch_extract_with_limit(libpgs_path, input_path, selected_indices,
                               tracks, track_modes, track_tags, tonemap,
                               nocrop, out_dir, threads, max_ds,
                               preview_cache, start=None, end=None,
                               has_cues=False):
    """Extract selected tracks with a per-track limit.

    Tracks whose analysis cache already contains enough content display
    sets are rendered from cache.  The rest are streamed from libpgs
    with reader-side per-track limiting.

    When *has_cues* is True and multiple tracks need streaming, a
    progressive multi-pass strategy is used: as each track reaches its
    quota, libpgs is restarted with only the remaining tracks, avoiding
    seeks through completed tracks' MKV cue entries.  When False (or
    only one stream track), a single libpgs pass is used.

    When *start* or *end* is set, cache is bypassed because it contains
    display sets from the beginning of the file, not the target range.

    Returns total images saved across all tracks.
    """
    # Partition: tracks with sufficient cache vs those needing streaming.
    # When a time range is active, cache is from the wrong range — stream all.
    cached_indices = []
    stream_indices = []
    if start or end:
        stream_indices = list(selected_indices)
    else:
        for ti in selected_indices:
            cached = preview_cache.get(ti)
            content_ds = [d for d in (cached or []) if ds_has_content(d)]
            if len(content_ds) >= max_ds:
                cached_indices.append(ti)
            else:
                stream_indices.append(ti)

    results = {}  # ti -> saved count
    consumer_threads = []

    # --- Stream tracks via libpgs ---
    mark_done = None
    if stream_indices:
        track_ids = [tracks[ti]["track_id"] for ti in stream_indices]

        # Progressive multi-pass: when cues are available and multiple
        # tracks need streaming, restart libpgs with fewer tracks as
        # each fills its quota — avoids seeking through completed
        # tracks' cue entries, dramatically speeding up sparse tracks.
        if has_cues and len(stream_indices) > 1:
            try:
                iterators, reader, mark_done = \
                    stream_file_multi_track_progressive(
                        libpgs_path, input_path, track_ids, max_ds=max_ds,
                        start=start, end=end)
            except Exception as e:
                print(f"  {error('[error]')} Multi-track extraction failed: {e}")
                return 0
        else:
            try:
                iterators, reader, proc, mark_done = stream_file_multi_track(
                    libpgs_path, input_path, track_ids, max_ds=max_ds,
                    start=start, end=end)
            except Exception as e:
                print(f"  {error('[error]')} Multi-track extraction failed: {e}")
                return 0

        for ti in stream_indices:
            track = tracks[ti]
            folder_name = build_track_folder_name(ti, track)
            track_out = os.path.join(out_dir, folder_name)
            track_label = f"Stream {track['index']}: {track['language']}"
            if track["title"]:
                track_label += f' "{track["title"]}"'

            q_iter = iterators[track["track_id"]]
            tid = track["track_id"]

            def _consume(ti=ti, q_iter=q_iter, track_out=track_out,
                         track_label=track_label, tid=tid):
                try:
                    saved = process_display_sets(
                        q_iter, track_out, track_modes[ti], tonemap, nocrop,
                        limit=max_ds,
                        detection=tracks[ti].get("detection"),
                        input_name=os.path.basename(input_path),
                        track_name=track_label,
                        threads=threads,
                        track_tag=track_tags[ti],
                    )
                    results[ti] = saved
                finally:
                    if mark_done is not None:
                        mark_done(tid)

            t = threading.Thread(target=_consume)
            t.start()
            consumer_threads.append(t)

    # --- Cached tracks: render from analysis cache in parallel ---
    for ti in cached_indices:
        track = tracks[ti]
        folder_name = build_track_folder_name(ti, track)
        track_out = os.path.join(out_dir, folder_name)
        track_label = f"Stream {track['index']}: {track['language']}"
        if track["title"]:
            track_label += f' "{track["title"]}"'

        cached = preview_cache[ti]

        def _consume_cached(ti=ti, cached=cached, track_out=track_out,
                            track_label=track_label):
            saved = process_display_sets(
                cached, track_out, track_modes[ti], tonemap, nocrop,
                limit=max_ds,
                detection=tracks[ti].get("detection"),
                input_name=os.path.basename(input_path),
                track_name=track_label,
                threads=threads,
                track_tag=track_tags[ti],
            )
            results[ti] = saved

        t = threading.Thread(target=_consume_cached)
        t.start()
        consumer_threads.append(t)

    for t in consumer_threads:
        t.join()
    if stream_indices:
        reader.join()

    total = 0
    for ti in selected_indices:
        saved = results.get(ti, 0)
        total += saved
        if saved == 0:
            tag = track_tags[ti]
            print(f"  {dim(tag)}  No subtitles found.")
    return total


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def process_sup_file(sup_path: str, out_dir: str, mode: str,
                     tonemap: str, first, nocrop: bool,
                     libpgs_path: str = None,
                     input_name: str = None,
                     track_name: str = None,
                     threads: int = None,
                     interactive: bool = False,
                     start: str = None,
                     end: str = None) -> int:
    """Decode a .sup file and write PNGs to out_dir. Returns images saved."""
    display_sets = list(stream_file(libpgs_path, sup_path,
                                    start=start, end=end))
    total = sum(1 for ds in display_sets if ds_has_content(ds))
    print(f"  {info('Found')} {bold(str(total))} subtitle display sets {dim(f'({len(display_sets)} total incl. clears)')}")

    # Color space detection
    detection = detect_from_palettes(display_sets)
    v = detection["verdict"]
    if v == "hdr":
        print(f"  {badge_hdr('Mastered for HDR')}")
    elif v == "sdr":
        print(f"  {badge_sdr('Mastered for SDR')}")
    else:
        print(f"  {info('Detected:')} {format_detection(detection)}")

    if mode in ("validate", "validate-fast"):
        return 0

    # --- Interactive count prompt (top-level .sup invocations only) ---
    if interactive and first is None and sys.stdin.isatty():
        first = select_count_interactive_sup(total)
        print()

    if mode == "auto":
        mode = _resolve_auto_mode(detection)
        print(f"  {info('Mode:')} {bold(mode.upper())} {dim('(auto-detected)')}  |  {info('Tonemap:')} {tonemap}  |  {info('Output:')} {out_dir}/")
    else:
        if (detection["verdict"] is not None
                and detection["verdict"] != mode
                and mode in ("hdr", "sdr")):
            print(f"  {warn('WARNING:')} --mode {mode} specified but {detection['verdict'].upper()} "
                  f"content detected. Subtitles may appear incorrect.")
        print(f"  {info('Mode:')} {bold(mode.upper())}  |  {info('Tonemap:')} {tonemap}  |  {info('Output:')} {out_dir}/")

    return process_display_sets(display_sets, out_dir, mode, tonemap, nocrop,
                                limit=first, detection=detection,
                                input_name=input_name or os.path.basename(sup_path),
                                track_name=track_name,
                                threads=threads)


def process_container(input_path: str, out_dir: str, mode: str,
                      tonemap: str, first, nocrop: bool,
                      libpgs_path: str = None,
                      tracks_arg: str = None,
                      threads: int = None,
                      start: str = None,
                      end: str = None) -> None:
    """Extract and decode PGS tracks from a video container.

    All extraction is performed via libpgs streaming — no temp files.
    When a display-set limit is active (--first or interactive default),
    the libpgs pipe is closed early once enough display sets are collected.
    """
    # === Phase 1: Discover tracks via libpgs ===
    print(f"{info('Probing:')} {input_path}")
    # When a time range is active, the discovery process (starting at
    # byte 0) can't be reused for targeted extraction — disable keep_alive.
    _keep_alive = start is None
    if _keep_alive:
        raw_tracks, kept_proc = discover_tracks(libpgs_path, input_path,
                                                keep_alive=True)
    else:
        raw_tracks = discover_tracks(libpgs_path, input_path,
                                     keep_alive=False)
        kept_proc = None

    if not raw_tracks:
        print(warn("No PGS subtitle tracks found."))
        return

    # Build track dicts from libpgs metadata.
    # has_cues: if any track lacks cues, disable multi-pass restart
    # (restarts without cues re-read from the beginning).
    has_cues = all(t.get("indexed") is True for t in raw_tracks)

    # For files with Cues, we don't need the discover process — a fresh
    # libpgs invocation with specific track IDs can seek efficiently.
    # For files without Cues, reuse the process to avoid re-reading
    # from the start (which can take seconds over NAS).
    if has_cues and kept_proc is not None:
        try:
            kept_proc.stdout.close()
        except Exception:
            pass
        if kept_proc.poll() is None:
            kept_proc.kill()
        kept_proc.wait()
        kept_proc = None
    tracks = []
    for t in raw_tracks:
        tracks.append({
            "index":      t["track_id"],
            "track_id":   t["track_id"],
            "language":   t.get("language") or "und",
            "title":      t.get("name") or "",
            "forced":     bool(t.get("is_forced")),
            "default":    bool(t.get("is_default")),
            "num_frames": t.get("display_set_count"),
        })

    # Video range detection via ffprobe (advisory only).
    ffprobe_path = check_ffprobe()
    video_range = probe_video_range(ffprobe_path, input_path) if ffprobe_path else None

    # === Phase 2: Single-pass analysis ===
    preview_cache = {}  # ti -> list of display sets
    all_indices = list(range(len(tracks)))

    if mode == "validate":
        _analyze_tracks(tracks, all_indices, libpgs_path, input_path,
                        preview_cache, budget=None, has_cues=has_cues,
                        reuse_proc=kept_proc, reuse_tracks=raw_tracks)
    else:
        _analyze_tracks(tracks, all_indices, libpgs_path, input_path,
                        preview_cache,
                        budget=Budget(LISTING_BUDGET_S), has_cues=has_cues,
                        reuse_proc=kept_proc, reuse_tracks=raw_tracks)

    # === Phase 3: Display track listing ===
    has_bailed = _print_track_listing(tracks, video_range=video_range)

    if mode in ("validate", "validate-fast"):
        if has_bailed and sys.stdin.isatty():
            if confirm_validate_bailed():
                bailed_indices = [
                    i for i, t in enumerate(tracks)
                    if t.get("analysis_bailed")
                ]
                _analyze_tracks(tracks, bailed_indices, libpgs_path,
                                input_path, preview_cache,
                                budget=None, has_cues=has_cues)
                print(CURSOR_UP_CLEAR, end="", flush=True)
                _print_track_listing(tracks, video_range=video_range)
        return

    # === Phase 4: Track selection (with [v] validate for bailed tracks) ===
    if tracks_arg is not None:
        if tracks_arg.lower() == "all":
            selected_indices = list(range(len(tracks)))
        else:
            try:
                selected_indices = [int(x.strip()) for x in tracks_arg.split(",")]
                selected_indices = [i for i in selected_indices if 0 <= i < len(tracks)]
            except ValueError:
                selected_indices = list(range(len(tracks)))
            if not selected_indices:
                print(f"  {warn('No valid track numbers.')} Processing all tracks.")
                selected_indices = list(range(len(tracks)))
    elif sys.stdin.isatty():
        if len(tracks) == 1 and not any(t.get("analysis_bailed") for t in tracks):
            selected_indices = [0]
        else:
            while True:
                any_bailed = any(t.get("analysis_bailed") for t in tracks)
                selection = select_tracks_interactive(tracks, has_bailed=any_bailed)
                if selection == "validate":
                    bailed_indices = [
                        i for i, t in enumerate(tracks)
                        if t.get("analysis_bailed")
                    ]
                    _analyze_tracks(tracks, bailed_indices, libpgs_path,
                                    input_path, preview_cache,
                                    budget=None, has_cues=has_cues)
                    print(CURSOR_UP_CLEAR, end="", flush=True)
                    has_bailed = _print_track_listing(tracks, video_range=video_range)
                    continue
                selected_indices = selection
                break
    else:
        selected_indices = list(range(len(tracks)))

    # --- Display-set count ---
    if first is not None:
        max_ds = first
    elif sys.stdin.isatty():
        max_ds = select_count_interactive()
    else:
        max_ds = None  # process all — backward-compatible default

    # When a time range is active, the analysis cache contains display
    # sets from the beginning of the file — not the target range.
    # Replace "cached" with a concrete count so we stream from the range.
    if (start or end) and max_ds == "cached":
        max_ds = DEFAULT_INTERACTIVE_COUNT

    # --- Resolve mode per track ---
    if mode == "auto":
        track_modes = {}
        for ti in selected_indices:
            track_modes[ti] = _resolve_auto_mode(
                tracks[ti].get("detection", {"verdict": None})
            )

        unique = set(track_modes.values())
        if len(unique) == 1:
            mode_note = f"{next(iter(unique)).upper()} (auto-detected)"
        else:
            per = ", ".join(
                f"track {ti}: {track_modes[ti].upper()}"
                for ti in selected_indices
            )
            mode_note = f"AUTO (per-track: {per})"
    elif mode in ("hdr", "sdr"):
        track_modes = {ti: mode for ti in selected_indices}
        mode_note = mode.upper()
        for ti in selected_indices:
            det = tracks[ti].get("detection", {})
            if det.get("verdict") and det["verdict"] != mode:
                print(f"  {warn('WARNING:')} --mode {mode} specified but track {ti} "
                      f"detected as {det['verdict'].upper()}. "
                      f"Subtitles may appear incorrect.")
    else:
        track_modes = {ti: mode for ti in selected_indices}
        mode_note = mode.upper()

    print()
    track_desc = ", ".join(str(i) for i in selected_indices)
    if max_ds == "cached":
        count_desc = f"up to {DEFAULT_INTERACTIVE_COUNT} cached"
    elif max_ds is not None:
        count_desc = str(max_ds)
    else:
        count_desc = "all"
    print(f"{info('Processing')} track(s) [{track_desc}], {count_desc} subtitle(s) each.")
    print(f"{info('Mode:')} {bold(mode_note)}  |  {info('Tonemap:')} {tonemap}  |  {info('Output:')} {out_dir}/")
    print()

    # === Phase 5: Extraction & rendering ===

    track_tags = _build_track_tags(tracks, selected_indices)

    total_saved = 0

    # Batch path (no display-set limit) with multiple tracks:
    # single-pass demuxed extraction avoids redundant MKV header / cues
    # parsing and (for containers without cues) re-reading the file.
    if max_ds is None and len(selected_indices) > 1:
        total_saved = _batch_extract_no_cues(
            libpgs_path, input_path, selected_indices, tracks,
            track_modes, track_tags, tonemap, nocrop, out_dir, threads,
            start=start, end=end)
        print()
        print(f"{success('Done.')} {total_saved} total images across "
              f"{len(selected_indices)} track(s) in {out_dir}/")
        return

    # Batch path with per-track limit and multiple tracks: single libpgs
    # pass with reader-side limiting.  Tracks with enough cached analysis
    # data are rendered from cache without streaming.
    if (max_ds is not None and max_ds != "cached"
            and len(selected_indices) > 1):
        total_saved = _batch_extract_with_limit(
            libpgs_path, input_path, selected_indices, tracks,
            track_modes, track_tags, tonemap, nocrop, out_dir, threads,
            max_ds, preview_cache, start=start, end=end,
            has_cues=has_cues)
        print()
        print(f"{success('Done.')} {total_saved} total images across "
              f"{len(selected_indices)} track(s) in {out_dir}/")
        return

    # Sequential path: single track, or cache-only mode.
    for ti in selected_indices:
        track = tracks[ti]
        folder_name = build_track_folder_name(ti, track)
        track_out = os.path.join(out_dir, folder_name)
        tag = track_tags[ti]

        cached = preview_cache.get(ti)
        content_ds = ([d for d in cached if ds_has_content(d)]
                      if cached else [])

        track_label = f"Stream {track['index']}: {track['language']}"
        if track["title"]:
            track_label += f' "{track["title"]}"'

        if max_ds is not None and max_ds == "cached":
            # Cache-only mode: use whatever was collected during analysis.
            if not content_ds:
                print(f"  {dim(tag)}  {dim('No cached subtitles. Skipping.')}")
                continue
            display_sets = cached
            effective_limit = DEFAULT_INTERACTIVE_COUNT
        elif max_ds is not None:
            # Streaming path with limit (single track).
            # When a time range is active, cache is from the wrong range.
            if not (start or end) and len(content_ds) >= max_ds:
                display_sets = cached
            else:
                try:
                    display_sets = list(stream_file(
                        libpgs_path, input_path,
                        track_id=track["track_id"],
                        max_ds=max_ds,
                        start=start, end=end,
                    ))
                except Exception as e:
                    print(f"  {error('[error]')} Streaming extraction failed: {e}")
                    continue
            effective_limit = max_ds
        else:
            # Unlimited, single track.
            try:
                ds_iter = stream_file(
                    libpgs_path, input_path,
                    track_id=track["track_id"],
                    start=start, end=end,
                )
            except Exception as e:
                print(f"  {error('[error]')} Extraction failed: {e}")
                continue
            saved = process_display_sets(
                ds_iter, track_out, track_modes[ti], tonemap, nocrop,
                limit=None,
                detection=tracks[ti].get("detection"),
                input_name=os.path.basename(input_path),
                track_name=track_label,
                threads=threads,
                track_tag=tag,
            )
            total_saved += saved
            if saved == 0:
                print(f"  {dim(tag)}  No subtitles found.")
            continue

        if not display_sets:
            print(f"  {dim(tag)}  No subtitles found.")
            continue

        content_total = sum(1 for d in display_sets if ds_has_content(d))
        if max_ds == "cached" and content_total > effective_limit:
            print(f"  {dim(tag)}  Collected {bold(str(content_total))} subtitle(s),"
                  f" rendering first {bold(str(effective_limit))}.")
        saved = process_display_sets(
            display_sets, track_out, track_modes[ti], tonemap, nocrop,
            limit=effective_limit,
            detection=tracks[ti].get("detection"),
            input_name=os.path.basename(input_path),
            track_name=track_label,
            threads=threads,
            track_tag=tag,
        )
        total_saved += saved

    print()
    print(f"{success('Done.')} {total_saved} total images across "
          f"{len(selected_indices)} track(s) in {out_dir}/")
