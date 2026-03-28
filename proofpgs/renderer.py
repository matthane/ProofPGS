"""Display Set rendering and PNG output."""

import os
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .style import warn, dim, info

_ASSETS = Path(__file__).resolve().parent / "assets"

_DEFAULT_MAX_THREADS = 8

from . import __version__
from .parser import ds_has_content
from .color import decode_palette_hdr, decode_palette_sdr


def _resolve_threads(threads):
    """Normalise thread count: None -> auto (up to 8), explicit -> clamped >=1."""
    if threads is not None:
        return max(1, threads)
    return min(os.cpu_count() or 1, _DEFAULT_MAX_THREADS)


_CompareResources = namedtuple("_CompareResources", [
    "label_font", "footer_font", "logo", "logo_w", "logo_h",
    "footer_text", "footer_color", "source_prefix",
    "filename_line_h", "footer_h", "min_panel_w",
    "detected_side", "check_icon", "x_icon",
    "icon_r", "icon_w", "green", "red", "gap", "tonemap",
])


def render_ds(ds: dict, mode: str, tonemap: str) -> tuple:
    """Render one Display Set to a PIL RGBA image.
    Returns (Image | None, pts_ms).
    """
    comp    = ds.get("composition")
    palette = ds.get("palettes", {})
    objects = ds.get("objects", {})
    pts_ms  = ds.get("pts_ms", 0.0)

    if not comp or not palette or not objects:
        return None, pts_ms
    if not comp["objects"]:
        return None, pts_ms

    # Build colour LUT
    if mode == "hdr":
        lut = decode_palette_hdr(palette, tonemap)
    else:
        lut = decode_palette_sdr(palette)

    # Render onto full-frame canvas
    canvas_w = comp["video_width"]
    canvas_h = comp["video_height"]
    canvas   = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    for placement in comp["objects"]:
        obj_id = placement["object_id"]
        x_pos  = placement["x"]
        y_pos  = placement["y"]

        if obj_id not in objects:
            continue
        obj = objects[obj_id]
        w, h = obj["width"], obj["height"]
        if not w or not h:
            continue

        try:
            indices = np.frombuffer(obj["bitmap"], dtype=np.uint8).reshape(h, w)
        except Exception as e:
            print(f"  {warn('[warn]')} Bitmap decode error obj {obj_id}: {e}")
            continue

        rgba    = lut[indices]                          # (h, w, 4) uint8
        obj_img = Image.fromarray(rgba, mode="RGBA")
        canvas.paste(obj_img, (x_pos, y_pos), mask=obj_img)

    return canvas, pts_ms


def crop_to_content(img: Image.Image, pad: int = 8) -> Image.Image:
    """Crop a full-frame RGBA image down to its non-transparent content."""
    arr = np.array(img)
    alpha = arr[:, :, 3]
    rows  = np.any(alpha > 0, axis=1)
    cols  = np.any(alpha > 0, axis=0)
    if not rows.any():
        return img
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    return img.crop((
        max(0, cmin - pad),
        max(0, rmin - pad),
        min(img.width,  cmax + 1 + pad),
        min(img.height, rmax + 1 + pad),
    ))


def _render_check_icon(r, color):
    """Render a smooth check-circle icon via 4x oversampling.

    Reproduces the Phosphor 'check-circle' regular icon: an outlined
    ring with a proportional checkmark, drawn at 4x resolution and
    downsampled with LANCZOS for clean antialiasing.
    """
    scale = 4
    sz = (2 * r + 1) * scale
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = sz // 2
    sr = r * scale
    lw = max(sr // 7, 2)
    d.ellipse([(c - sr, c - sr), (c + sr, c + sr)],
              outline=color, width=lw)
    # Checkmark proportions from the Phosphor SVG path (256-unit viewBox,
    # vertices at roughly (-46,+2), (-16,+32), (+45,-30) relative to
    # centre, normalised to the scaled radius).
    d.line([
        (c - int(sr * 0.35), c + int(sr * 0.05)),
        (c - int(sr * 0.10), c + int(sr * 0.30)),
        (c + int(sr * 0.40), c - int(sr * 0.30)),
    ], fill=color, width=lw)
    return img.resize((2 * r + 1, 2 * r + 1), Image.LANCZOS)


def _render_x_icon(r, color):
    """Render a smooth x-circle icon via 4x oversampling.

    Reproduces the Phosphor 'x-circle' regular icon: an outlined ring
    with a proportional X, drawn at 4x resolution and downsampled with
    LANCZOS for clean antialiasing.
    """
    scale = 4
    sz = (2 * r + 1) * scale
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = sz // 2
    sr = r * scale
    lw = max(sr // 7, 2)
    d.ellipse([(c - sr, c - sr), (c + sr, c + sr)],
              outline=color, width=lw)
    dd = int(sr * 0.35)
    d.line([(c - dd, c - dd), (c + dd, c + dd)], fill=color, width=lw)
    d.line([(c - dd, c + dd), (c + dd, c - dd)], fill=color, width=lw)
    return img.resize((2 * r + 1, 2 * r + 1), Image.LANCZOS)


def _build_compare_resources(detection, tonemap, input_name, track_name):
    """Build the immutable resource bundle used by compare-mode workers."""
    label_font = ImageFont.truetype(str(_ASSETS / "Sora-Medium.ttf"), 14)

    detected_side = None
    if detection and detection.get("verdict"):
        detected_side = detection["verdict"]

    icon_r = 8
    green = (100, 200, 100, 255)
    red   = (200, 100, 100, 255)

    check_icon = None
    x_icon = None
    icon_w = 0
    gap = 12

    _sdr_w = label_font.getlength("BT.709 (SDR DECODE)")
    _hdr_w = label_font.getlength(f"BT.2020+PQ -> BT.709 ({tonemap.upper()})")
    if detected_side:
        check_icon = _render_check_icon(icon_r, green)
        x_icon     = _render_x_icon(icon_r, red)
        icon_w = 2 * icon_r + 1
        _ind_w = gap + icon_w + 6 + int(
            label_font.getlength("NOT MASTERED FOR HDR"))
        min_panel_w = int(max(_sdr_w, _hdr_w) + _ind_w) + 8
    else:
        min_panel_w = int(max(_sdr_w, _hdr_w)) + 8

    footer_font = ImageFont.truetype(str(_ASSETS / "Sora-Regular.ttf"), 14)
    logo_raw = Image.open(_ASSETS / "proofpgs-icon-footer.png").convert("RGBA")
    logo_h = 20
    logo_w = int(logo_raw.width * logo_h / logo_raw.height)
    logo = logo_raw.resize((logo_w, logo_h), Image.LANCZOS)
    footer_text = f"ProofPGS v{__version__}"
    footer_color = (100, 100, 100, 255)
    source_prefix_parts = [p for p in (input_name, track_name) if p]
    source_prefix = ("  \u2022  ".join(source_prefix_parts)
                     if source_prefix_parts else None)
    filename_line_h = 24 if source_prefix else 0
    footer_h = 42 + filename_line_h

    return _CompareResources(
        label_font=label_font, footer_font=footer_font,
        logo=logo, logo_w=logo_w, logo_h=logo_h,
        footer_text=footer_text, footer_color=footer_color,
        source_prefix=source_prefix,
        filename_line_h=filename_line_h, footer_h=footer_h,
        min_panel_w=min_panel_w, detected_side=detected_side,
        check_icon=check_icon, x_icon=x_icon,
        icon_r=icon_r, icon_w=icon_w,
        green=green, red=red, gap=gap, tonemap=tonemap,
    )


def _render_and_save(ds, i, out_dir, mode, tonemap, nocrop):
    """Worker: render one display set and save PNG.

    Returns (i, pts_ms, fname) or (i, pts_ms, None) on skip.
    """
    try:
        img, pts_ms = render_ds(ds, mode, tonemap)
    except Exception:
        return (i, 0.0, None)
    if img is None:
        return (i, pts_ms, None)
    if not nocrop:
        img = crop_to_content(img)

    fname = f"ds_{i:04d}_{pts_ms:.0f}ms_{mode}.png"
    arr = np.array(img)
    if arr[:, :, 3].min() < 255:
        img.save(os.path.join(out_dir, fname))
    else:
        img.convert("RGB").save(os.path.join(out_dir, fname))
    return (i, pts_ms, fname)


def _render_and_save_compare(ds, i, out_dir, nocrop, res):
    """Worker: render one display set in compare mode and save PNG.

    Returns (i, pts_ms, fname) or (i, pts_ms, None) on skip.
    """
    try:
        img_sdr, pts_ms = render_ds(ds, "sdr", res.tonemap)
        img_hdr, _      = render_ds(ds, "hdr", res.tonemap)
    except Exception:
        return (i, 0.0, None)
    if img_sdr is None and img_hdr is None:
        return (i, pts_ms, None)

    if not nocrop:
        ref_img = img_hdr or img_sdr
        arr = np.array(ref_img)
        alpha = arr[:, :, 3]
        rows = np.any(alpha > 0, axis=1)
        cols = np.any(alpha > 0, axis=0)
        if rows.any():
            pad = 8
            rmin, rmax = np.where(rows)[0][[0, -1]]
            cmin, cmax = np.where(cols)[0][[0, -1]]
            box = (
                max(0, cmin - pad), max(0, rmin - pad),
                min(ref_img.width,  cmax + 1 + pad),
                min(ref_img.height, rmax + 1 + pad),
            )
            if img_sdr: img_sdr = img_sdr.crop(box)
            if img_hdr: img_hdr = img_hdr.crop(box)

    ref = img_hdr or img_sdr
    w, h = ref.width, ref.height
    w = max(w, res.min_panel_w)

    pad = 10
    label_h = 34
    gutter = pad * 4
    total_w = pad * 2 + w * 2 + gutter
    footer_margin = pad * 2
    combined = Image.new("RGBA",
                         (total_w, pad * 2 + label_h + h + footer_margin + res.footer_h),
                         (20, 20, 20, 255))
    draw = ImageDraw.Draw(combined)

    sdr_x = pad
    hdr_x = pad + w + gutter

    sdr_label = "BT.709 (SDR DECODE)"
    hdr_label = f"BT.2020+PQ -> BT.709 ({res.tonemap.upper()})"
    text_y = pad + 6
    draw.text((sdr_x + 4, text_y), sdr_label,
              fill=(180, 180, 180, 255), font=res.label_font)
    draw.text((hdr_x + 4, text_y), hdr_label,
              fill=(180, 180, 180, 255), font=res.label_font)

    if res.detected_side:
        sdr_icon_x = (sdr_x + 4
                      + int(draw.textlength(sdr_label, font=res.label_font))
                      + res.gap)
        if res.detected_side == "sdr":
            combined.paste(res.check_icon, (sdr_icon_x, text_y),
                           mask=res.check_icon)
            draw.text((sdr_icon_x + res.icon_w + 6, text_y),
                      "MASTERED FOR SDR", fill=res.green, font=res.label_font)
        else:
            combined.paste(res.x_icon, (sdr_icon_x, text_y),
                           mask=res.x_icon)
            draw.text((sdr_icon_x + res.icon_w + 6, text_y),
                      "NOT MASTERED FOR SDR", fill=res.red, font=res.label_font)

        hdr_icon_x = (hdr_x + 4
                      + int(draw.textlength(hdr_label, font=res.label_font))
                      + res.gap)
        if res.detected_side == "hdr":
            combined.paste(res.check_icon, (hdr_icon_x, text_y),
                           mask=res.check_icon)
            draw.text((hdr_icon_x + res.icon_w + 6, text_y),
                      "MASTERED FOR HDR", fill=res.green, font=res.label_font)
        else:
            combined.paste(res.x_icon, (hdr_icon_x, text_y),
                           mask=res.x_icon)
            draw.text((hdr_icon_x + res.icon_w + 6, text_y),
                      "NOT MASTERED FOR HDR", fill=res.red, font=res.label_font)

    img_y = pad + label_h
    if img_sdr:
        combined.paste(img_sdr, (sdr_x, img_y), mask=img_sdr)
    if img_hdr:
        combined.paste(img_hdr, (hdr_x, img_y), mask=img_hdr)

    div_x = pad + w + gutter // 2
    draw.line([(div_x, img_y), (div_x, img_y + h)],
              fill=(60, 60, 60, 255), width=1)

    footer_base = pad + label_h + h + footer_margin

    if res.source_prefix:
        total_s = int(pts_ms // 1000)
        h_ts, m_ts = divmod(total_s, 3600)
        m_ts, s_ts = divmod(m_ts, 60)
        ms_frac = int(pts_ms % 1000)
        timestamp = f"{h_ts:d}:{m_ts:02d}:{s_ts:02d}.{ms_frac:03d}"
        source_line = res.source_prefix + "  \u2022  " + timestamp
        src_w = int(draw.textlength(source_line, font=res.footer_font))
        src_x = (total_w - src_w) // 2
        draw.text((src_x, footer_base + 6),
                  source_line, fill=res.footer_color, font=res.footer_font)

    logo_top = footer_base + res.filename_line_h + (42 - res.logo_h) // 2
    text_w = int(draw.textlength(res.footer_text, font=res.footer_font))
    content_gap = 6
    content_w = res.logo_w + content_gap + text_w
    cx = (total_w - content_w) // 2
    combined.paste(res.logo, (cx, logo_top), mask=res.logo)
    draw.text((cx + res.logo_w + content_gap, logo_top + 1),
              res.footer_text, fill=res.footer_color, font=res.footer_font)

    fname = f"ds_{i:04d}_{pts_ms:.0f}ms_compare.png"
    combined.convert("RGB").save(os.path.join(out_dir, fname))
    return (i, pts_ms, fname)


def process_display_sets(display_sets, out_dir: str, mode: str,
                         tonemap: str, nocrop: bool,
                         limit: int = None, detection: dict = None,
                         input_name: str = None,
                         track_name: str = None,
                         threads: int = None,
                         track_tag: str = None) -> int:
    """Render display sets and save PNGs to out_dir.

    *display_sets* may be a list or any iterable (including a generator).
    When a generator is passed, rendering starts as display sets arrive
    rather than waiting for extraction to finish — overlapping I/O-bound
    extraction with CPU-bound rendering.

    Args:
        limit:    Max number of *rendered* images to produce.  Display sets
                  that don't produce output (clears) don't count.
                  None means no limit.
        threads:  Number of parallel rendering threads.
                  None = auto (up to 8), 1 = sequential.
        track_tag: Short label (e.g. "eng") prefixed to each output line.
                   None omits the prefix (single-track / .sup files).

    Returns images saved.
    """
    os.makedirs(out_dir, exist_ok=True)
    num_threads = _resolve_threads(threads)

    # --- Build worker function ---
    if mode == "compare":
        res = _build_compare_resources(detection, tonemap, input_name,
                                       track_name)
        def _worker(item):
            return _render_and_save_compare(item[1], item[0], out_dir,
                                            nocrop, res)
    else:
        def _worker(item):
            return _render_and_save(item[1], item[0], out_dir, mode,
                                    tonemap, nocrop)

    tag_prefix = f"  {dim(track_tag)}  " if track_tag else "  "
    folder_hint = os.path.basename(out_dir)

    def _print_result(idx, pts_ms, fname):
        print(f"{tag_prefix}{dim(f'[{idx:04d}]')}  {pts_ms / 1000.0:8.3f}s  {dim('->')}  {info(f'{folder_hint}/{fname}')}")

    # --- Sequential fast path (no threading overhead) ---
    if num_threads <= 1:
        saved = 0
        content_count = 0
        for i, ds in enumerate(display_sets):
            idx, pts_ms, fname = _worker((i, ds))
            if fname is not None:
                saved += 1
                _print_result(idx, pts_ms, fname)
            if ds_has_content(ds):
                content_count += 1
                if limit is not None and content_count >= limit:
                    break
        return saved

    # --- Parallel path with ordered output ---
    # Consume the iterable incrementally, submitting work to the pool
    # as display sets arrive.  Results are buffered and printed in order.
    saved = 0
    results_buf = {}
    next_to_print = 0
    content_count = 0

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = {}  # future -> seq
        seq = 0

        for i, ds in enumerate(display_sets):
            fut = pool.submit(_worker, (i, ds))
            futures[fut] = seq
            seq += 1

            if ds_has_content(ds):
                content_count += 1
                if limit is not None and content_count >= limit:
                    break

            # Opportunistically drain completed futures (non-blocking)
            done_futs = [f for f in futures if f.done()]
            for f in done_futs:
                s = futures.pop(f)
                try:
                    results_buf[s] = f.result()
                except Exception:
                    results_buf[s] = (i, 0.0, None)

                while next_to_print in results_buf:
                    idx, pts_ms, fname = results_buf.pop(next_to_print)
                    if fname is not None:
                        saved += 1
                        _print_result(idx, pts_ms, fname)
                    next_to_print += 1

        # Drain remaining futures
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                results_buf[s] = fut.result()
            except Exception:
                results_buf[s] = (0, 0.0, None)

            while next_to_print in results_buf:
                idx, pts_ms, fname = results_buf.pop(next_to_print)
                if fname is not None:
                    saved += 1
                    _print_result(idx, pts_ms, fname)
                next_to_print += 1

    return saved
