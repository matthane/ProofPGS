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
from .ffmpeg import probe_video_stream, build_track_folder_name, check_ffprobe
from .interactive import (
    select_tracks_interactive, select_count_interactive,
    select_count_interactive_sup, confirm_validate_bailed,
)
from .constants import (Budget, LISTING_BUDGET_S, ANALYSIS_MAX_DS,
                        DEFAULT_INTERACTIVE_COUNT)
from .style import (
    warn, error, dim, dim_bold, bold,
    box_top, box_bottom, box_row, box_blank, box_sep, status_ok,
    glyph, BOX_WIDTH,
    CURSOR_UP_CLEAR,
)



def _track_label(track: dict) -> str:
    """Build a human-readable label like ``Stream 3: English "Commentary"``."""
    label = f"Stream {track['index']}: {track['language']}"
    if track["title"]:
        label += f' "{track["title"]}"'
    return label


def _resolve_auto_mode(detection: dict) -> str:
    """Resolve 'auto' mode using a detection result. Returns resolved mode."""
    if detection["verdict"] is not None:
        return detection["verdict"]
    return "compare"


def _fmt_mode(mode: str) -> str:
    """Format a mode name for display: HDR/SDR stay uppercase (acronyms),
    other modes are capitalized (``compare`` → ``Compare``)."""
    return mode.upper() if mode in ("hdr", "sdr") else mode.capitalize()


def _build_track_tags(tracks, selected_indices):
    """Build short per-line tags for each selected track.

    Format is ``index:lang`` (e.g. ``1:de``, ``3:en``), matching the
    ``[index]`` shown in the track listing so the user can cross-reference.
    """
    tags = {}
    for ti in selected_indices:
        tags[ti] = f"{ti + 1}:{tracks[ti]['language']}"
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

    _basename = os.path.basename(input_path)

    def _print_timer_label(num_validated=0):
        label = (f"{bold('Analyzing')} {num_tracks} PGS track(s) in "
                 f"{_basename}...")
        if num_validated > 0:
            label += f" {dim(f'({num_validated}/{num_tracks} validated)')}"
        return label

    _timer_label = [_print_timer_label()]  # mutable for thread access

    def _tick():
        while not _timer_stop.wait(0.5):
            if _is_tty:
                e = time.monotonic() - _timer_t0
                print(f"\r{_timer_label[0]} {dim(f'{e:.0f}s')}", end="", flush=True)

    print()
    # Only announce the fallback path — the cues-available happy path is
    # silent so the track listing dominates the output.
    if not has_cues:
        if budget:
            print(f"  {dim(f'No subtitle cues; sequential scan ({budget.limit:.0f}s budget).')}")
        else:
            print(f"  {dim('No subtitle cues; sequential scan.')}")
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

            # Composition size — read from the first display set with a
            # valid composition. Per UHD BD spec, the graphics plane is
            # fixed at 1920x1080 for the whole track, so the first is
            # representative.
            for d in ds:
                comp = d.get("composition")
                if comp is not None:
                    t["composition_size"] = (comp["video_width"],
                                             comp["video_height"])
                    break

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


def _print_track_listing(tracks, video_info=None):
    """Print the track listing with analysis results.

    *video_info* is a dict ``{"range": "hdr"|"sdr", "width": int,
    "height": int}`` from the container's video stream probe, or None.
    When a subtitle track's detected range differs from ``video_info["range"]``,
    a "Dynamic range mismatch" badge is appended.

    Returns True if any tracks were bailed (not analyzed).
    """
    has_bailed = False

    video_range = video_info["range"] if video_info else None

    # Determine index-column width so `[1]` / `[10]` stay aligned.
    idx_w = max(len(f"[{ti}]") for ti in range(1, len(tracks) + 1))

    lines = ["", box_top()]

    # Pre-check whether any track has a dynamic range mismatch.
    any_mismatch = (
        video_range is not None
        and any(
            not t.get("analysis_bailed")
            and t.get("detection", {}).get("verdict") is not None
            and t["detection"]["verdict"] != video_range
            for t in tracks
        )
    )

    # Video stream header row (if known) + a blank row of breathing space.
    if video_info is not None:
        vs_text = f" {dim('Video stream:')} {video_range.upper()}"
        vh = video_info.get("height") or 0
        if vh >= 2160:
            vs_label = "4K"
        elif vh >= 1080:
            vs_label = "1080p"
        elif vh >= 720:
            vs_label = "720p"
        elif vh > 0:
            vw = video_info.get("width") or 0
            vs_label = f"{vw}\u00d7{vh}"
        else:
            vs_label = None
        if vs_label:
            vs_text += f" ({vs_label})"
        if any_mismatch:
            vs_text += f"   {warn(glyph('warn') + ' Dynamic range mismatch detected')}"
        lines.append(box_row(vs_text))
        lines.append(box_blank())

    for ti, t in enumerate(tracks):
        det = t.get("detection", {})

        # Does this track's range differ from the video stream?
        is_mismatch = (
            video_range is not None
            and not t.get("analysis_bailed")
            and det.get("verdict") is not None
            and det["verdict"] != video_range
        )

        # --- First row: [i]  HDR/SDR • Language • "Title" [flags] ---
        idx_raw = f"[{ti + 1}]".ljust(idx_w)

        detail_parts = []

        if t.get("analysis_bailed"):
            has_bailed = True
            detail_parts.append(dim("not analyzed *"))
        elif det.get("verdict") == "hdr":
            detail_parts.append(warn(f"{glyph('warn')} HDR") if is_mismatch else "HDR")
        elif det.get("verdict") == "sdr":
            detail_parts.append(warn(f"{glyph('warn')} SDR") if is_mismatch else "SDR")
        elif det.get("verdict"):
            detail_parts.append(det["verdict"].upper())
        else:
            detail_parts.append(dim("unknown"))

        detail_parts.append(t["language"])
        if t["title"]:
            detail_parts.append(f'"{t["title"]}"')
        flags = []
        if t["forced"]:
            flags.append("forced")
        if t["default"]:
            flags.append("default")
        if flags:
            detail_parts.append(f"[{', '.join(flags)}]")

        sep = f"  {dim(glyph('dot'))}  "
        detail = sep.join(detail_parts)
        if t.get("analysis_bailed") or det.get("verdict") is None:
            identity = f" {dim(idx_raw)}  {dim(detail)}"
        else:
            identity = f" {bold(idx_raw)}  {detail}"
        lines.append(box_row(identity))

        # --- Second row: stream N • ~N subs ---
        attr_indent = " " + (" " * idx_w) + "  "
        attr_parts = [dim(f"stream {t['index']}")]

        num_frames = t.get("num_frames")
        if num_frames and num_frames > 0:
            approx_subs = max(1, num_frames // 2)
            attr_parts.append(dim(f"~{approx_subs:,} subs"))

        comp_size = t.get("composition_size")
        if comp_size:
            _comp_labels = {
                (3840, 2160): "4K",
                (1920, 1080): "1080p",
                (1280, 720): "720p",
            }
            comp_label = _comp_labels.get(
                comp_size, f"{comp_size[0]}\u00d7{comp_size[1]}")
            attr_parts.append(dim(comp_label))

        if t.get("indexed"):
            attr_parts.append(dim("indexed"))

        attr_sep = f"  {dim(glyph('dot'))}  "
        attr_line = attr_indent + attr_sep.join(attr_parts)
        lines.append(box_row(attr_line))

        if ti < len(tracks) - 1:
            lines.append(box_sep())

    # Footer: ProofPGS vX.Y.Z right-aligned.  Name is bold+dim, version
    # stays plain dim so the brand label reads a touch stronger than the
    # version suffix without pulling attention from the track list above.
    from . import __version__
    version_str = f"v{__version__}"
    footer_plain = f"ProofPGS {version_str}"
    footer_styled = f"{dim_bold('ProofPGS')} {dim(version_str)}"
    inner = BOX_WIDTH - 4  # content area width inside "│ " ... " │"
    lead = " " * max(0, inner - len(footer_plain))
    lines.append(box_blank())
    lines.append(box_row(lead + footer_styled))
    lines.append(box_bottom())

    for line in lines:
        print(line)

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
        print(f"  {error(f'Multi-track extraction failed: {e}')}")
        return 0

    results = {}  # ti -> saved count
    consumer_threads = []

    for ti in selected_indices:
        track = tracks[ti]
        folder_name = build_track_folder_name(ti, track)
        track_out = os.path.join(out_dir, folder_name)
        track_label = _track_label(track)

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
                print(f"  {error(f'Multi-track extraction failed: {e}')}")
                return 0
        else:
            try:
                iterators, reader, proc, mark_done = stream_file_multi_track(
                    libpgs_path, input_path, track_ids, max_ds=max_ds,
                    start=start, end=end)
            except Exception as e:
                print(f"  {error(f'Multi-track extraction failed: {e}')}")
                return 0

        for ti in stream_indices:
            track = tracks[ti]
            folder_name = build_track_folder_name(ti, track)
            track_out = os.path.join(out_dir, folder_name)
            track_label = _track_label(track)

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
        track_label = _track_label(track)

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
    manifest = {}

    def _on_header(hdr):
        manifest.update(hdr)

    # Phase 1: Analysis — cap at ANALYSIS_MAX_DS content display sets
    # (matches container behavior; libpgs exits the pipe early). Request
    # the manifest header so we get the total display-set count from
    # libpgs's cheap pre-scan rather than having to stream the whole file.
    analysis_ds = list(stream_file(libpgs_path, sup_path,
                                   start=start, end=end,
                                   max_ds=ANALYSIS_MAX_DS,
                                   on_header=_on_header,
                                   with_header=True))

    detection = detect_from_palettes(analysis_ds)
    v = detection["verdict"]

    # Totals come from the manifest pre-scan. If the libpgs binary is too
    # old to emit the header, fall back to streaming the full file once
    # for the count — and reuse that list for rendering so we only pay
    # for one full read.
    full_ds = None
    if manifest.get("total_content_display_sets") is not None:
        total = manifest["total_content_display_sets"]
        total_all = manifest.get("total_display_sets", total)
    else:
        full_ds = list(stream_file(libpgs_path, sup_path,
                                   start=start, end=end))
        total = sum(1 for ds in full_ds if ds_has_content(ds))
        total_all = len(full_ds)

    # Detection label for the box row
    if v == "hdr":
        det_label = "HDR"
    elif v == "sdr":
        det_label = "SDR"
    else:
        det_label = format_detection(detection)

    count_desc = f"{total} subtitle display sets"
    if total_all != total:
        count_desc += f" {dim(f'({total_all} total incl. clears)')}"

    sep = f"  {dim(glyph('dot'))}  "
    summary_row = f" {det_label}{sep}{count_desc}"

    # Version footer (same pattern as _print_track_listing)
    from . import __version__
    version_str = f"v{__version__}"
    footer_plain = f"ProofPGS {version_str}"
    footer_styled = f"{dim_bold('ProofPGS')} {dim(version_str)}"
    inner = BOX_WIDTH - 4
    lead = " " * max(0, inner - len(footer_plain))

    print()
    print(box_top())
    print(box_row(summary_row))
    print(box_blank())
    print(box_row(lead + footer_styled))
    print(box_bottom())
    print()

    if mode in ("validate", "validate-fast"):
        return 0

    # --- Interactive count prompt (top-level .sup invocations only) ---
    if interactive and first is None and sys.stdin.isatty():
        first = select_count_interactive_sup(total)
        print()

    if mode == "auto":
        mode = _resolve_auto_mode(detection)
        print(f"{bold('Mode:')} {_fmt_mode(mode)} {dim('(auto-detected)')}  |  {bold('Tonemap:')} {tonemap.capitalize()}  |  {bold('Output:')} {out_dir}/")
    else:
        if (detection["verdict"] is not None
                and detection["verdict"] != mode
                and mode in ("hdr", "sdr")):
            det_label = detection["verdict"].upper()
            print("  " + warn(
                f"Warning: --mode {mode} specified but {det_label} "
                f"content detected. Subtitles may appear incorrect."
            ))
        print(f"{bold('Mode:')} {_fmt_mode(mode)}  |  {bold('Tonemap:')} {tonemap.capitalize()}  |  {bold('Output:')} {out_dir}/")
    print()

    # Phase 2: Render — reuse the analysis cache when it already holds
    # enough content, otherwise stream fresh. If we had to fall back to a
    # full pre-scan (old libpgs binary), reuse that list directly.
    analysis_content = sum(1 for ds in analysis_ds if ds_has_content(ds))
    if full_ds is not None:
        render_ds = full_ds
    elif first is not None and first <= analysis_content:
        render_ds = analysis_ds
    else:
        render_ds = stream_file(libpgs_path, sup_path,
                                start=start, end=end)

    return process_display_sets(render_ds, out_dir, mode, tonemap, nocrop,
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
            "indexed":    bool(t.get("indexed")),
        })

    # Video stream probe via ffprobe (advisory only).
    ffprobe_path = check_ffprobe()
    video_info = probe_video_stream(ffprobe_path, input_path) if ffprobe_path else None

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
    has_bailed = _print_track_listing(tracks, video_info=video_info)

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
                _print_track_listing(tracks, video_info=video_info)
        return

    # === Phase 4: Track selection (with [v] validate for bailed tracks) ===
    if tracks_arg is not None:
        if tracks_arg.lower() == "all":
            selected_indices = list(range(len(tracks)))
        else:
            try:
                # User input is 1-based; convert to 0-based internal indices.
                selected_indices = [int(x.strip()) - 1 for x in tracks_arg.split(",")]
                selected_indices = [i for i in selected_indices if 0 <= i < len(tracks)]
            except ValueError:
                selected_indices = list(range(len(tracks)))
            if not selected_indices:
                print(f"  {warn('No valid track numbers. Processing all tracks.')}")
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
                    has_bailed = _print_track_listing(tracks, video_info=video_info)
                    continue
                selected_indices = selection
                break
    else:
        selected_indices = list(range(len(tracks)))

    # --- Display-set count ---
    if first is not None:
        max_ds = first
    elif sys.stdin.isatty():
        max_ds = select_count_interactive(has_cues=has_cues)
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
            mode_note = f"{_fmt_mode(next(iter(unique)))} (auto-detected)"
        else:
            per = ", ".join(
                f"track {ti + 1}: {_fmt_mode(track_modes[ti])}"
                for ti in selected_indices
            )
            mode_note = f"Auto (per-track: {per})"
    elif mode in ("hdr", "sdr"):
        track_modes = {ti: mode for ti in selected_indices}
        mode_note = _fmt_mode(mode)
        for ti in selected_indices:
            det = tracks[ti].get("detection", {})
            if det.get("verdict") and det["verdict"] != mode:
                det_label = _fmt_mode(det["verdict"])
                print("  " + warn(
                    f"Warning: --mode {mode} specified but track {ti + 1} "
                    f"detected as {det_label}. Subtitles may appear incorrect."
                ))
    else:
        track_modes = {ti: mode for ti in selected_indices}
        mode_note = _fmt_mode(mode)

    print()
    track_desc = ", ".join(str(i + 1) for i in selected_indices)
    if max_ds == "cached":
        count_desc = ("1 cached subtitle each" if has_cues
                      else f"up to {DEFAULT_INTERACTIVE_COUNT} cached subtitle(s) each")
    elif max_ds is not None:
        count_desc = (f"{max_ds} subtitle each" if max_ds == 1
                      else f"{max_ds} subtitles each")
    else:
        count_desc = "all subtitles"
    print(f"{bold('Processing')} track(s) [{track_desc}], {count_desc}.")
    print()
    print(f"{bold('Mode:')} {mode_note}  |  {bold('Tonemap:')} {tonemap.capitalize()}  |  {bold('Output:')} {out_dir}/")
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
        print(status_ok(f"{total_saved} total images across "
                        f"{len(selected_indices)} track(s) in {out_dir}/"))
        print()
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
        print(status_ok(f"{total_saved} total images across "
                        f"{len(selected_indices)} track(s) in {out_dir}/"))
        print()
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

        track_label = _track_label(track)

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
                    print(f"  {error(f'Streaming extraction failed: {e}')}")
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
                print(f"  {error(f'Extraction failed: {e}')}")
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
    print(status_ok(f"{total_saved} total images across "
                    f"{len(selected_indices)} track(s) in {out_dir}/"))
    print()
