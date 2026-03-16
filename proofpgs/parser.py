"""PGS binary format parsing: .sup file reading, segment parsers, RLE decoder."""

import struct

import numpy as np

from .constants import SEG_END, SEG_ODS, format_time
from .style import warn


# ---------------------------------------------------------------------------
# .sup file / stream readers
# ---------------------------------------------------------------------------

def read_sup(path: str) -> list:
    """Parse a .sup file into a list of Display Sets.
    Each DS is a list of {type, pts, payload} dicts.
    """
    with open(path, "rb") as f:
        data = f.read()

    pos = 0
    display_sets = []
    current_ds = []

    while pos + 13 <= len(data):
        if data[pos:pos + 2] != b"PG":
            print(f"{warn('[warn]')} Bad magic at {pos:#010x}, stopping.")
            break

        pts      = struct.unpack(">I", data[pos + 2:pos + 6])[0]
        seg_type = data[pos + 10]
        seg_size = struct.unpack(">H", data[pos + 11:pos + 13])[0]
        payload  = data[pos + 13: pos + 13 + seg_size]

        current_ds.append({"type": seg_type, "pts": pts, "payload": payload})

        if seg_type == SEG_END:
            display_sets.append(current_ds)
            current_ds = []

        pos += 13 + seg_size

    return display_sets


def ds_has_content(ds: list) -> bool:
    """Check if a display set contains renderable subtitle content.

    PGS subtitles use paired display sets: one to show (with ODS bitmap
    data) and one to clear (PCS with num_objects=0, no ODS).  Only the
    "show" sets produce a visible PNG.  We detect content by checking
    for at least one Object Definition Segment (ODS / 0x15).
    """
    return any(seg["type"] == SEG_ODS for seg in ds)


def read_sup_streaming(stream, max_ds: int = None) -> list:
    """Parse PGS segments incrementally from a binary stream.

    When reading from an FFmpeg pipe, stopping early causes FFmpeg to
    receive a broken-pipe signal and exit — so it only reads through the
    container file as far as needed.  This is the key optimisation: if
    the user only wants the first 10 subtitles, FFmpeg stops after
    reaching the 10th subtitle's position in the file.

    Only display sets that contain actual subtitle content (ODS segments)
    count toward max_ds.  "Clear" display sets (used to hide the previous
    subtitle) are collected but do not count, so max_ds=10 yields exactly
    10 renderable subtitles.

    Args:
        stream:  A binary readable (e.g. subprocess stdout pipe).
        max_ds:  Stop after collecting this many *content* display sets.
                 None means read everything.

    Returns:
        List of display sets (same format as read_sup).
    """
    display_sets = []
    current_ds = []
    content_count = 0
    showed_progress = False

    while True:
        header = stream.read(13)
        if len(header) < 13:
            break

        if header[0:2] != b"PG":
            print(f"{warn('[warn]')} Bad magic in stream, stopping.")
            break

        pts      = struct.unpack(">I", header[2:6])[0]
        seg_type = header[10]
        seg_size = struct.unpack(">H", header[11:13])[0]

        payload = stream.read(seg_size) if seg_size > 0 else b""
        if len(payload) < seg_size:
            break

        current_ds.append({"type": seg_type, "pts": pts, "payload": payload})

        if seg_type == SEG_END:
            display_sets.append(current_ds)
            has_content = ds_has_content(current_ds)
            current_ds = []
            if has_content:
                content_count += 1
            # Show position in the file so user knows FFmpeg is still
            # working — especially important for sparse forced tracks
            # where subtitles are spread across the whole movie.
            pos_s = pts / 90_000.0
            pos_str = format_time(pos_s)
            if max_ds is not None:
                print(f"\r  Streaming: {content_count}/{max_ds} subtitles "
                      f"(at {pos_str} in file)   ",
                      end="", flush=True)
                showed_progress = True
                if content_count >= max_ds:
                    break
            else:
                if content_count % 50 == 0 and content_count > 0:
                    print(f"\r  Streaming: {content_count} subtitles "
                          f"(at {pos_str} in file)   ",
                          end="", flush=True)
                    showed_progress = True

    if showed_progress:
        print()  # newline after progress
    return display_sets


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
