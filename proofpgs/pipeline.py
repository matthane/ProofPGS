"""High-level orchestration: process .sup files and video containers."""

import os
import sys
import threading
import time

from .parser import ds_has_content
from .renderer import process_display_sets
from .detect import detect_from_palettes, format_detection
from .libpgs import stream_file, discover_tracks, stream_all_tracks
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


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _analyze_tracks(tracks, track_indices, libpgs_path, input_path,
                    preview_cache, budget=None):
    """Single-pass analysis: stream samples for all requested tracks at
    once via libpgs, then run detection on each.

    Updates each track dict in-place with:
      detection, analysis_bailed
    Caches extracted display sets in *preview_cache*.
    """
    if not track_indices:
        return

    extract_tracks = [tracks[ti] for ti in track_indices]

    # Live elapsed-time display on the "Analyzing" line.
    _timer_stop = threading.Event()
    _timer_t0 = time.monotonic()
    _timer_label = f"  {info('Analyzing')} {len(extract_tracks)} PGS track(s)..."
    _is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def _tick():
        while not _timer_stop.wait(0.5):
            if _is_tty:
                e = time.monotonic() - _timer_t0
                print(f"\r{_timer_label} {dim(f'{e:.0f}s')}", end="", flush=True)

    _timer_thread = threading.Thread(target=_tick, daemon=True)
    print(_timer_label, end="", flush=True)
    _timer_thread.start()

    try:
        # Content-based watchdog: stop streaming as soon as all tracks
        # have conclusive detection.
        def ready_check(track_data):
            for ti in track_indices:
                t = tracks[ti]
                tid = t["track_id"]
                ds = track_data.get(tid, [])
                if not ds:
                    return False
                det = detect_from_palettes(ds)
                if det["verdict"] is None:
                    return False
            return True

        track_data = stream_all_tracks(
            libpgs_path, input_path,
            max_ds_per_track=ANALYSIS_MAX_DS,
            deadline=budget.deadline() if budget else None,
            ready_check=ready_check,
        )

        for ti in track_indices:
            t = tracks[ti]
            tid = t["track_id"]
            ds = track_data.get(tid, [])

            # Cap to ANALYSIS_MAX_DS display sets.
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
        # Always stop the timer — including on KeyboardInterrupt.
        _timer_stop.set()
        try:
            _timer_thread.join(timeout=1)
        except (KeyboardInterrupt, OSError):
            pass
        elapsed = time.monotonic() - _timer_t0
        if _is_tty:
            print(f"\r{_timer_label} {dim(f'{elapsed:.1f}s')}")
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
# Public entry points
# ---------------------------------------------------------------------------

def process_sup_file(sup_path: str, out_dir: str, mode: str,
                     tonemap: str, first, nocrop: bool,
                     libpgs_path: str = None,
                     input_name: str = None,
                     track_name: str = None,
                     threads: int = None,
                     interactive: bool = False) -> int:
    """Decode a .sup file and write PNGs to out_dir. Returns images saved."""
    display_sets = stream_file(libpgs_path, sup_path)
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
                      threads: int = None) -> None:
    """Extract and decode PGS tracks from a video container.

    All extraction is performed via libpgs streaming — no temp files.
    When a display-set limit is active (--first or interactive default),
    the libpgs pipe is closed early once enough display sets are collected.
    """
    # === Phase 1: Discover tracks via libpgs ===
    print(f"{info('Probing:')} {input_path}")
    raw_tracks = discover_tracks(libpgs_path, input_path)

    if not raw_tracks:
        print(warn("No PGS subtitle tracks found."))
        return

    # Build track dicts from libpgs metadata.
    tracks = []
    for t in raw_tracks:
        tracks.append({
            "index":      t["track_id"],
            "track_id":   t["track_id"],
            "language":   t.get("language") or "und",
            "title":      t.get("name") or "",
            "forced":     bool(t.get("flag_forced")),
            "default":    bool(t.get("flag_default")),
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
                        preview_cache, budget=None)
    else:
        _analyze_tracks(tracks, all_indices, libpgs_path, input_path,
                        preview_cache,
                        budget=Budget(LISTING_BUDGET_S))

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
                                budget=None)
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
                                budget=None)
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

    total_saved = 0

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

        if max_ds is not None and max_ds == "cached":
            # Cache-only mode: use whatever was collected during analysis.
            if not content_ds:
                print(f"  {dim('No cached subtitles for this track. Skipping.')}")
                print()
                continue
            display_sets = cached
            effective_limit = DEFAULT_INTERACTIVE_COUNT
        elif max_ds is not None:
            # Streaming path with limit.
            if len(content_ds) >= max_ds:
                # Reuse cached analysis data when it has enough content.
                display_sets = cached
            else:
                try:
                    display_sets = stream_file(
                        libpgs_path, input_path,
                        track_id=track["track_id"],
                        max_ds=max_ds,
                        show_progress=True,
                    )
                except Exception as e:
                    print(f"  {error('[error]')} Streaming extraction failed: {e}")
                    continue
            effective_limit = max_ds
        else:
            # Batch path: stream all display sets for this track.
            try:
                display_sets = stream_file(
                    libpgs_path, input_path,
                    track_id=track["track_id"],
                    show_progress=True,
                )
            except Exception as e:
                print(f"  {error('[error]')} Extraction failed: {e}")
                continue
            effective_limit = None

        if not display_sets:
            print("  No subtitles found.")
            print()
            continue

        content_total = sum(1 for d in display_sets if ds_has_content(d))
        if max_ds == "cached" and content_total > effective_limit:
            print(f"  Collected {bold(str(content_total))} subtitle(s),"
                  f" rendering first {bold(str(effective_limit))}.")
        else:
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

    print(f"{success('Done.')} {total_saved} total images across "
          f"{len(selected_indices)} track(s) in {out_dir}/")
