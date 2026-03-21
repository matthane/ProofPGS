"""PGS segment parsers and RLE decoder."""

import struct

import numpy as np

from .constants import SEG_ODS, SEG_PDS


def rle_used_entries(rle_data: bytes) -> set:
    """Scan RLE data and return the set of palette entry IDs actually used.

    This is a lightweight alternative to full RLE decoding — it walks the
    RLE stream collecting colour indices without expanding to a pixel array.
    Used by detection to filter out palette entries that are defined but
    never referenced by the bitmap (common in fade-in frames).
    """
    used = set()
    pos = 0
    n = len(rle_data)

    while pos < n:
        b1 = rle_data[pos]; pos += 1

        if b1 != 0x00:
            used.add(b1)
        else:
            if pos >= n:
                break
            b2 = rle_data[pos]; pos += 1

            if b2 == 0x00:
                continue  # end-of-line
            elif b2 & 0x40:
                # Long run
                pos += 1  # skip length low byte
                if b2 & 0x80:
                    if pos < n:
                        used.add(rle_data[pos])
                    pos += 1
            else:
                # Short run
                if b2 & 0x80:
                    if pos < n:
                        used.add(rle_data[pos])
                    pos += 1

    return used


def ds_has_content(ds: list) -> bool:
    """Check if a display set contains renderable subtitle content.

    PGS subtitles use paired display sets: one to show (with ODS bitmap
    data) and one to clear (PCS with num_objects=0, no ODS).  Only the
    "show" sets produce a visible PNG.  We detect content by checking
    for at least one Object Definition Segment (ODS / 0x15).
    """
    return any(seg["type"] == SEG_ODS for seg in ds)


def pts_to_ms(pts: int) -> float:
    return pts / 90.0


# ---------------------------------------------------------------------------
# Segment parsers
# ---------------------------------------------------------------------------

def parse_pcs(payload: bytes) -> dict:
    if len(payload) < 11:
        return {}
    return {
        "width":          struct.unpack(">H", payload[0:2])[0],
        "height":         struct.unpack(">H", payload[2:4])[0],
        "comp_state":     payload[7],
        "palette_update": payload[8],
        "palette_id":     payload[9],
        "num_objects":    payload[10],
        "raw":            payload,   # kept for composition object parsing
    }


def parse_pds(payload: bytes) -> dict:
    """Parse Palette Definition Segment.
    Returns dict mapping entry_id -> (Y, Cr, Cb, Alpha).
    NOTE: Per the PGS spec the order is Y, Cr, Cb — not Y, Cb, Cr.
    """
    entries = {}
    i = 2  # skip palette_id, version
    while i + 4 < len(payload):
        eid        = payload[i]
        Y, Cr, Cb  = payload[i + 1], payload[i + 2], payload[i + 3]
        alpha      = payload[i + 4]
        entries[eid] = (Y, Cr, Cb, alpha)
        i += 5
    return entries


def parse_ods(payload: bytes) -> dict:
    """Parse Object Definition Segment. Returns metadata + raw RLE bytes."""
    if len(payload) < 7:
        return {}
    obj_id   = struct.unpack(">H", payload[0:2])[0]
    seq_flag = payload[3]
    width = height = None
    if seq_flag & 0x80:       # First (or only) fragment — has dimensions
        if len(payload) < 11:
            return {}
        width  = struct.unpack(">H", payload[7:9])[0]
        height = struct.unpack(">H", payload[9:11])[0]
        rle    = payload[11:]
    else:
        rle = payload[4:]
    return {"obj_id": obj_id, "seq_flag": seq_flag,
            "width": width, "height": height, "rle": rle}


# ---------------------------------------------------------------------------
# RLE decoder
# ---------------------------------------------------------------------------

def decode_rle(rle_data: bytes, width: int, height: int) -> np.ndarray:
    """Decode PGS run-length encoded bitmap to a 2D palette-index array."""
    pixels = np.zeros(width * height, dtype=np.uint8)
    pos = 0
    out = 0
    n   = len(rle_data)

    while pos < n and out < width * height:
        b1 = rle_data[pos]; pos += 1

        if b1 != 0x00:
            # Single pixel of colour b1
            pixels[out] = b1
            out += 1
        else:
            if pos >= n:
                break
            b2 = rle_data[pos]; pos += 1

            if b2 == 0x00:
                # End-of-line — advance to next row boundary
                col = out % width
                if col:
                    out += width - col

            elif b2 & 0x40:
                # Long run (length encoded across two bytes)
                if pos >= n:
                    break
                b3 = rle_data[pos]; pos += 1
                run_len = ((b2 & 0x3F) << 8) | b3
                colour  = 0
                if b2 & 0x80:
                    if pos >= n:
                        break
                    colour = rle_data[pos]; pos += 1
                end = min(out + run_len, width * height)
                pixels[out:end] = colour
                out = end

            else:
                # Short run
                run_len = b2 & 0x3F
                colour  = 0
                if b2 & 0x80:
                    if pos >= n:
                        break
                    colour = rle_data[pos]; pos += 1
                end = min(out + run_len, width * height)
                pixels[out:end] = colour
                out = end

    return pixels.reshape(height, width)
