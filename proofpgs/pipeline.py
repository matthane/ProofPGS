"""High-level orchestration: process .sup files and video containers."""

import os
import shutil
import subprocess
import sys
import tempfile

from .parser import read_sup, ds_has_content
from .renderer import process_display_sets
from .detect import detect_from_palettes, format_detection
from .ffmpeg import (
    check_ffmpeg, probe_pgs_tracks,
    extract_all_pgs_tracks, build_track_folder_name,
    extract_track_streaming, extract_analysis_samples,
)
from .interactive import select_tracks_interactive, select_count_interactive, confirm_validate_bailed
from .constants import Budget, LISTING_BUDGET_S, ANALYSIS_MAX_DS, TS_SEGMENTS_PER_DS
from .style import (
    info, warn, error, success, heading, dim, bold,
    badge_hdr, badge_sdr, badge_compare, badge_unknown,
    CURSOR_UP_CLEAR,
)



def _check_all_detected(paths):
    """True when every track's temp file has conclusive SDR/HDR detection.

    Called by the content-based watchdog in validate mode to decide when
    FFmpeg can be killed early.  Reads temp .sup files that FFmpeg is
    actively writing to — safe because ``-flush_packets 1`` ensures
    complete PGS segments on disk, and ``read_sup`` discards any
    incomplete trailing display set.
    """
    for p in paths:
        try:
            if not os.path.isfile(p) or os.path.getsize(p) == 0:
                return False
            ds = read_sup(p)
            if not ds:
                return False
            det = detect_from_palettes(ds)
            if det["verdict"] is None:
                return False
        except Exception:
            return False
    return True


def _adjust_pts_offset(display_sets, start_time_s):
    """Subtract container start_time from all segment PTS values.

    Blu-ray M2TS streams have a non-zero initial PTS offset (the transport
    stream clock doesn't start at zero).  MKV files remuxed from the same
    disc start at ~0.  Subtracting start_time normalises both so that PTS
    represents elapsed time from the start of the content.
    """
    if not start_time_s or start_time_s <= 0:
        return
    offset = int(start_time_s * 90_000)
    for ds in display_sets:
        for seg in ds:
            seg["pts"] = max(0, seg["pts"] - offset)


def _resolve_auto_mode(detection: dict) -> str:
    """Resolve 'auto' mode using a detection result. Returns resolved mode."""
    if detection["verdict"] is not None:
        return detection["verdict"]
    return "compare"


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _analyze_tracks(tracks, track_indices, ffmpeg_path, input_path,
                    duration_s, preview_cache, budget=None):
    """Single-pass analysis: extract samples for all requested tracks at
    once, then run detection on each.

    Updates each track dict in-place with:
      detection, analysis_bailed
    Caches extracted display sets in *preview_cache*.
    """
    if not track_indices:
        return

    # Determine seek position — mid-file for long files, none for short.
    has_duration = duration_s is not None and duration_s > 0
    is_short = not has_duration or duration_s <= 120
    seek_s = None if is_short else max(0, (duration_s / 2) - 60)

    extract_tracks = [tracks[ti] for ti in track_indices]

    # Transport streams (M2TS/TS) demux one PGS segment per packet,
    # while MKV/MP4 pack a full display set into each packet.  Scale
    # the packet cap so both formats yield ~the same number of DS.
    ext = os.path.splitext(input_path)[1].lower()
    if ext in (".m2ts", ".ts", ".mts"):
        max_packets = ANALYSIS_MAX_DS * TS_SEGMENTS_PER_DS
    else:
        max_packets = ANALYSIS_MAX_DS

    label = "Validating" if budget is None else "Analyzing"
    print(f"  {info(label)} {len(extract_tracks)} PGS track(s)...")

    # Content-based watchdog: kill FFmpeg as soon as all tracks have
    # conclusive detection.  In budgeted mode the deadline acts as a
    # fallback for sparse tracks; in validate mode there is no deadline.
    ready_fn = _check_all_detected

    # --- Single FFmpeg pass for all tracks ---
    temp_dir = tempfile.mkdtemp(prefix="pgs_analysis_")
    try:
        sup_paths = extract_analysis_samples(
            ffmpeg_path, input_path, extract_tracks, temp_dir,
            seek_s=seek_s,
            max_packets=max_packets,
            deadline=budget.deadline() if budget else None,
            ready_check=ready_fn,
        )

        for list_i, ti in enumerate(track_indices):
            t = tracks[ti]
            sup_path = sup_paths[list_i]

            if os.path.isfile(sup_path) and os.path.getsize(sup_path) > 0:
                ds = read_sup(sup_path)
            else:
                ds = []

            # Cap to ANALYSIS_MAX_DS display sets so sample sizes are
            # consistent across container formats.  For MKV, -frames:s
            # already limits to this many DS.  For M2TS, the scaled
            # packet cap may yield slightly more DS (clear DS have
            # fewer segments), so truncate to keep the sample uniform.
            if len(ds) > ANALYSIS_MAX_DS:
                ds = ds[:ANALYSIS_MAX_DS]

            preview_cache[ti] = ds
            content_count = sum(1 for d in ds if ds_has_content(d))

            # --- Color space detection ---
            if ds:
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
        shutil.rmtree(temp_dir, ignore_errors=True)

    # --- Retry inconclusive detections from start of file ---
    if seek_s is not None:
        can_retry = budget is None or not budget.exhausted()
        retry_indices = [
            ti for ti in track_indices
            if tracks[ti]["detection"]["verdict"] is None and can_retry
        ]
        if retry_indices:
            retry_tracks = [tracks[ti] for ti in retry_indices]
            temp_dir2 = tempfile.mkdtemp(prefix="pgs_retry_")
            try:
                sup_paths2 = extract_analysis_samples(
                    ffmpeg_path, input_path, retry_tracks, temp_dir2,
                    seek_s=None,  # from start of file
                    max_packets=max_packets,
                    deadline=budget.deadline() if budget else None,
                    ready_check=ready_fn,
                )
                for list_i, ti in enumerate(retry_indices):
                    t = tracks[ti]
                    sup_path = sup_paths2[list_i]
                    if (os.path.isfile(sup_path)
                            and os.path.getsize(sup_path) > 0):
                        ds = read_sup(sup_path)
                        fallback = detect_from_palettes(ds)
                        if fallback["verdict"] is not None:
                            t["detection"] = fallback
                        # Update cache if we now have data for a
                        # previously-bailed track.
                        content_count = sum(
                            1 for d in ds if ds_has_content(d)
                        )
                        if content_count > 0:
                            preview_cache[ti] = ds
                            t["analysis_bailed"] = False
            finally:
                shutil.rmtree(temp_dir2, ignore_errors=True)


def _print_track_listing(tracks):
    """Print the track listing with analysis results.

    Returns True if any tracks were bailed (not analyzed).
    """
    has_bailed = False

    # --- Pass 1: build plain-text columns for width calculation ---
    rows = []  # (index_col, stream_col, detail_col, badge_plain, badge_styled)

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

        rows.append((index_col, stream_col, detail_col,
                     badge_plain, badge_styled))

    # --- Compute column widths ---
    idx_w    = max((len(r[0]) for r in rows), default=0)
    stream_w = max((len(r[1]) for r in rows), default=0)
    detail_w = max((len(r[2]) for r in rows), default=0)

    # --- Pass 2: print aligned ---
    print(f"{info('Found')} {bold(str(len(tracks)))} PGS subtitle track(s):")
    for index_col, stream_col, detail_col, badge_plain, badge_styled in rows:
        idx_part = bold(index_col.ljust(idx_w))
        stream_part = dim(stream_col.ljust(stream_w))
        if badge_plain:
            detail_part = detail_col.ljust(detail_w)
            print(f"  {idx_part}  {stream_part}  {detail_part}  {badge_styled}")
        else:
            print(f"  {idx_part}  {stream_part}  {detail_col}")

    if has_bailed:
        print()
        print(dim("  * Very sparse tracks with few subtitles may require reading"))
        print(dim("    deep into the file and take longer."))
    print()

    return has_bailed


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def process_sup_file(sup_path: str, out_dir: str, mode: str,
                     tonemap: str, first, nocrop: bool,
                     input_name: str = None,
                     track_name: str = None,
                     threads: int = None,
                     start_time_s: float = None) -> int:
    """Decode a .sup file and write PNGs to out_dir. Returns images saved."""
    display_sets = read_sup(sup_path)
    _adjust_pts_offset(display_sets, start_time_s)
    total = sum(1 for ds in display_sets if ds_has_content(ds))
    print(f"  {info('Found')} {bold(str(total))} subtitle display sets {dim(f'({len(display_sets)} total incl. clears)')}")

    # Color space detection
    detection = detect_from_palettes(display_sets)
    det_str = format_detection(detection)
    print(f"  {info('Detected:')} {det_str}")

    if mode in ("validate", "validate-fast"):
        return 0

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
                      tracks_arg: str = None,
                      threads: int = None) -> None:
    """Extract and decode PGS tracks from a video container.

    When a display-set limit is active (--first or interactive default),
    uses streaming extraction: FFmpeg pipes each track's PGS data to
    stdout and is terminated early once enough display sets are collected.
    This avoids reading the entire container file and creates no temp files.

    When processing all display sets, uses batch extraction to temp files
    (single FFmpeg pass for all selected tracks) for maximum efficiency.
    """
    ffmpeg_path, ffprobe_path = check_ffmpeg()

    print(f"{info('Probing:')} {input_path}")
    tracks, duration_s, start_time_s = probe_pgs_tracks(ffprobe_path, input_path)

    if not tracks:
        print(warn("No PGS subtitle tracks found."))
        return

    # === Phase 2: Single-pass analysis (target: <10s wallclock) ===
    preview_cache = {}  # ti -> list of display sets
    all_indices = list(range(len(tracks)))

    if mode == "validate":
        _analyze_tracks(tracks, all_indices, ffmpeg_path, input_path,
                        duration_s, preview_cache, budget=None)
    else:
        budget = Budget(LISTING_BUDGET_S)
        _analyze_tracks(tracks, all_indices, ffmpeg_path, input_path,
                        duration_s, preview_cache, budget=budget)

    # === Phase 3: Display track listing ===
    # Clear the "Validating/Analyzing" status line now that results are ready.
    print(CURSOR_UP_CLEAR, end="", flush=True)
    has_bailed = _print_track_listing(tracks)

    if mode in ("validate", "validate-fast"):
        if mode == "validate-fast" and has_bailed and sys.stdin.isatty():
            if confirm_validate_bailed():
                bailed_indices = [
                    i for i, t in enumerate(tracks)
                    if t.get("analysis_bailed")
                ]
                _analyze_tracks(tracks, bailed_indices, ffmpeg_path,
                                input_path, duration_s, preview_cache,
                                budget=None)
                print(CURSOR_UP_CLEAR, end="", flush=True)
                _print_track_listing(tracks)
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
        while True:
            any_bailed = any(t.get("analysis_bailed") for t in tracks)
            selection = select_tracks_interactive(tracks, has_bailed=any_bailed)
            if selection == "validate":
                bailed_indices = [
                    i for i, t in enumerate(tracks)
                    if t.get("analysis_bailed")
                ]
                _analyze_tracks(tracks, bailed_indices, ffmpeg_path,
                                input_path, duration_s, preview_cache,
                                budget=None)
                print(CURSOR_UP_CLEAR, end="", flush=True)
                has_bailed = _print_track_listing(tracks)
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
        count_desc = "cached"
    elif max_ds is not None:
        count_desc = str(max_ds)
    else:
        count_desc = "all"
    print(f"{info('Processing')} track(s) [{track_desc}], {count_desc} subtitle(s) each.")
    print(f"{info('Mode:')} {bold(mode_note)}  |  {info('Tonemap:')} {tonemap}  |  {info('Output:')} {out_dir}/")
    print()

    # === Phase 5: Extraction & rendering ===

    total_saved = 0

    if max_ds is not None:
        # ---- Streaming path: pipe per track, stop early ----
        mid_seek = None
        if duration_s and duration_s > 0:
            mid_seek = max(0, (duration_s / 2) - 60)

        for ti in selected_indices:
            track = tracks[ti]
            folder_name = build_track_folder_name(ti, track)
            track_out = os.path.join(out_dir, folder_name)

            title_str = f' "{track["title"]}"' if track["title"] else ""
            print(heading(f"=== Track {ti}: {track['language']}{title_str} "
                          f"(stream {track['index']}) ==="))

            cached = preview_cache.get(ti)
            content_ds = ([d for d in cached if ds_has_content(d)]
                          if cached else [])

            if max_ds == "cached":
                # Cache-only mode: use whatever was collected during analysis.
                if not content_ds:
                    print(f"  {dim('No cached subtitles for this track. Skipping.')}")
                    print()
                    continue
                display_sets = cached
                effective_limit = None  # render all cached DS
            else:
                # Reuse cached analysis data when it has enough content.
                if len(content_ds) >= max_ds:
                    display_sets = cached
                else:
                    try:
                        display_sets = extract_track_streaming(
                            ffmpeg_path, input_path, track["index"], max_ds,
                            seek_s=mid_seek,
                        )
                    except Exception as e:
                        print(f"  {error('[error]')} Streaming extraction failed: {e}")
                        continue
                effective_limit = max_ds

            if not display_sets:
                print("  No subtitles found.")
                continue

            _adjust_pts_offset(display_sets, start_time_s)
            content_total = sum(1 for d in display_sets if ds_has_content(d))
            print(f"  Collected {bold(str(content_total))} subtitle(s).")
            track_label = f"Stream {track['index']}: {track['language']}"
            if track["title"]:
                track_label += f' "{track["title"]}"'
            saved = process_display_sets(
                display_sets, track_out, track_modes[ti], tonemap, nocrop,
                limit=effective_limit,
                detection=tracks[ti].get("detection"),
                input_name=os.path.basename(input_path),
                track_name=track_label,
                threads=threads,
            )
            total_saved += saved
            print()

    else:
        # ---- Batch path: extract selected tracks to temp files ----
        selected_track_list = [tracks[i] for i in selected_indices]
        temp_dir = tempfile.mkdtemp(prefix="pgs_extract_")

        try:
            try:
                sup_paths = extract_all_pgs_tracks(
                    ffmpeg_path, input_path, selected_track_list,
                    temp_dir, duration_s
                )
            except subprocess.CalledProcessError as e:
                print(f"{error('[error]')} ffmpeg extraction failed: {e}")
                return

            for enum_i, ti in enumerate(selected_indices):
                track = tracks[ti]
                folder_name = build_track_folder_name(ti, track)
                track_out = os.path.join(out_dir, folder_name)
                temp_sup = sup_paths[enum_i]

                title_str = f' "{track["title"]}"' if track["title"] else ""
                print(heading(f"=== Track {ti}: {track['language']}{title_str} "
                              f"(stream {track['index']}) ==="))

                if not os.path.isfile(temp_sup):
                    print(f"  {warn('[warn]')} Extraction produced no output "
                          f"for stream {track['index']}")
                    continue

                track_label = f"Stream {track['index']}: {track['language']}"
                if track["title"]:
                    track_label += f' "{track["title"]}"'
                saved = process_sup_file(
                    temp_sup, track_out, track_modes[ti], tonemap, None, nocrop,
                    input_name=os.path.basename(input_path),
                    track_name=track_label,
                    threads=threads,
                    start_time_s=start_time_s,
                )
                total_saved += saved
                print()

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"{success('Done.')} {total_saved} total images across "
          f"{len(selected_indices)} track(s) in {out_dir}/")
