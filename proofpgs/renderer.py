"""Display Set rendering and PNG output."""

import os
import struct

import numpy as np
from PIL import Image, ImageDraw

from .constants import SEG_PCS, SEG_PDS, SEG_ODS
from .parser import parse_pcs, parse_pds, parse_ods, decode_rle, pts_to_ms
from .color import decode_palette_hdr, decode_palette_sdr


def render_ds(ds: list, mode: str, tonemap: str) -> tuple:
    """Render one Display Set to a PIL RGBA image.
    Returns (Image | None, pts_ms).
    """
    pcs_data = None
    palette  = {}
    objects  = {}   # obj_id -> {width, height, rle}
    pts_ms   = 0.0

    for seg in ds:
        t = seg["type"]
        p = seg["payload"]

        if t == SEG_PCS:
            pcs_data = parse_pcs(p)
            pts_ms   = pts_to_ms(seg["pts"])

        elif t == SEG_PDS:
            palette.update(parse_pds(p))

        elif t == SEG_ODS:
            ods = parse_ods(p)
            if not ods:
                continue
            oid = ods["obj_id"]
            if oid not in objects:
                objects[oid] = {"width": None, "height": None, "rle": b""}
            # First fragment carries dimensions
            if ods["width"] is not None:
                objects[oid]["width"]  = ods["width"]
                objects[oid]["height"] = ods["height"]
            objects[oid]["rle"] += ods["rle"]

    if not pcs_data or not palette or not objects:
        return None, pts_ms
    if pcs_data["num_objects"] == 0:
        return None, pts_ms

    # Build colour LUT
    if mode == "hdr":
        lut = decode_palette_hdr(palette, tonemap)
    else:
        lut = decode_palette_sdr(palette)

    # Render onto full-frame canvas
    canvas_w = pcs_data["width"]
    canvas_h = pcs_data["height"]
    canvas   = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # Parse composition objects from PCS payload (offset 11, 8 bytes each, +8 if cropped)
    raw       = pcs_data["raw"]
    obj_off   = 11
    for _ in range(pcs_data["num_objects"]):
        if obj_off + 8 > len(raw):
            break
        obj_id    = struct.unpack(">H", raw[obj_off:obj_off + 2])[0]
        crop_flag = raw[obj_off + 3]
        x_pos     = struct.unpack(">H", raw[obj_off + 4:obj_off + 6])[0]
        y_pos     = struct.unpack(">H", raw[obj_off + 6:obj_off + 8])[0]
        obj_off  += 8
        if crop_flag & 0x40:
            obj_off += 8  # skip crop rectangle

        if obj_id not in objects:
            continue
        obj = objects[obj_id]
        w, h = obj["width"], obj["height"]
        if not w or not h:
            continue

        try:
            indices = decode_rle(obj["rle"], w, h)
        except Exception as e:
            print(f"  [warn] RLE decode error obj {obj_id}: {e}")
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


def process_display_sets(display_sets: list, out_dir: str, mode: str,
                         tonemap: str, nocrop: bool,
                         limit: int = None) -> int:
    """Render display sets and save PNGs to out_dir.

    Args:
        limit:  Max number of *rendered* images to produce.  Display sets
                that don't produce output (clears) don't count.
                None means no limit.

    Returns images saved.
    """
    os.makedirs(out_dir, exist_ok=True)
    saved = 0

    for i, ds in enumerate(display_sets):

        if mode == "compare":
            img_sdr, pts_ms = render_ds(ds, "sdr", tonemap)
            img_hdr, _      = render_ds(ds, "hdr", tonemap)
            if img_sdr is None and img_hdr is None:
                continue

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
            label_h = 22
            combined = Image.new("RGBA", (w * 2 + 10, h + label_h), (20, 20, 20, 255))
            draw = ImageDraw.Draw(combined)
            draw.text((4, 4),        "BT.709 (SDR decode)",              fill=(180, 180, 180, 255))
            draw.text((w + 14, 4),   f"BT.2020+PQ -> BT.709 ({tonemap})", fill=(180, 180, 180, 255))
            if img_sdr:
                combined.paste(img_sdr, (0,      label_h), mask=img_sdr)
            if img_hdr:
                combined.paste(img_hdr, (w + 10, label_h), mask=img_hdr)

            fname = f"ds_{i:04d}_{pts_ms:.0f}ms_compare.png"
            combined.convert("RGB").save(os.path.join(out_dir, fname))

        else:
            img, pts_ms = render_ds(ds, mode, tonemap)
            if img is None:
                continue
            if not nocrop:
                img = crop_to_content(img)

            fname = f"ds_{i:04d}_{pts_ms:.0f}ms.png"
            arr = np.array(img)
            if arr[:, :, 3].min() < 255:
                img.save(os.path.join(out_dir, fname))
            else:
                img.convert("RGB").save(os.path.join(out_dir, fname))

        saved += 1
        print(f"  [{i:04d}]  {pts_ms / 1000.0:8.3f}s  ->  {fname}")

        if limit is not None and saved >= limit:
            break

    return saved
