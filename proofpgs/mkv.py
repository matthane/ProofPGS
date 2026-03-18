"""Direct MKV extraction: parse EBML/Cues to read only subtitle clusters.

Instead of reading the entire container sequentially (40 GB+ for UHD
Blu-ray remuxes), this module reads the MKV Cues index (~10-100 KB) to
find the exact file positions of clusters containing PGS subtitle data,
then seeks directly to those clusters.  Total I/O drops from tens of GB
to a few MB.

Falls back to None on any structural issue (missing Cues, parse error)
so the caller can retry with FFmpeg.
"""

import os
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

from .style import info


# ---------------------------------------------------------------------------
# EBML element IDs (Matroska spec)
# ---------------------------------------------------------------------------

_ID_SEGMENT         = 0x18538067
_ID_SEEKHEAD        = 0x114D9B74
_ID_SEEK            = 0x4DBB
_ID_SEEKID          = 0x53AB
_ID_SEEKPOSITION    = 0x53AC
_ID_INFO            = 0x1549A966
_ID_TIMESTAMP_SCALE = 0x2AD7B1
_ID_TRACKS          = 0x1654AE6B
_ID_TRACKENTRY      = 0xAE
_ID_TRACKNUMBER     = 0xD7
_ID_TRACKTYPE       = 0x83
_ID_CODECID         = 0x86
_ID_CUES            = 0x1C53BB6B
_ID_CUEPOINT        = 0xBB
_ID_CUETIME         = 0xB3
_ID_CUETRACKPOS     = 0xB7
_ID_CUETRACK        = 0xF7
_ID_CUECLUSTERPOS   = 0xF1
_ID_CUERELPOS       = 0xF0   # CueRelativePosition
_ID_CLUSTER         = 0x1F43B675
_ID_CLUSTER_TS      = 0xE7
_ID_SIMPLEBLOCK     = 0xA3
_ID_BLOCKGROUP      = 0xA0
_ID_BLOCK           = 0xA1

# Master elements whose children we need to descend into.
_MASTER_IDS = {
    _ID_SEGMENT, _ID_SEEKHEAD, _ID_SEEK, _ID_INFO, _ID_TRACKS,
    _ID_TRACKENTRY, _ID_CUES, _ID_CUEPOINT, _ID_CUETRACKPOS,
    _ID_CLUSTER, _ID_BLOCKGROUP,
}

# Content encoding / compression
_ID_CONTENTENCODINGS  = 0x6D80
_ID_CONTENTENCODING   = 0x6240
_ID_CONTENTCOMPRESSION = 0x5034
_ID_CONTENTCOMPALGO   = 0x4254
_ID_CONTENTCOMPSETTINGS = 0x4255

# Compression algorithms (ContentCompAlgo values).
_COMP_ZLIB = 0            # default when ContentCompression is present
_COMP_HEADER_STRIP = 3

# PGS codec identifier in MKV.
_PGS_CODEC_ID = "S_HDMV/PGS"


# ---------------------------------------------------------------------------
# EBML variable-length integer helpers
# ---------------------------------------------------------------------------

def _read_element_id(data: bytes, pos: int) -> tuple[int, int]:
    """Decode an EBML element ID starting at *pos*.

    Returns (id_value, bytes_consumed).  The length-indicator bits are
    part of the ID value (unlike data sizes).
    """
    if pos >= len(data):
        raise ValueError("unexpected end of data reading element ID")
    first = data[pos]
    if first == 0:
        raise ValueError(f"invalid EBML ID at offset {pos}")

    # Count leading zero bits to determine byte width.
    if   first & 0x80: width = 1
    elif first & 0x40: width = 2
    elif first & 0x20: width = 3
    elif first & 0x10: width = 4
    else:
        raise ValueError(f"unsupported EBML ID width at offset {pos}")

    if pos + width > len(data):
        raise ValueError("truncated EBML element ID")

    value = 0
    for i in range(width):
        value = (value << 8) | data[pos + i]
    return value, width


def _read_vint_size(data: bytes, pos: int) -> tuple[int, int]:
    """Decode an EBML data-size vint starting at *pos*.

    Returns (size_value, bytes_consumed).  The length-indicator bits are
    masked off.  A value of all-1s means "unknown size".
    """
    if pos >= len(data):
        raise ValueError("unexpected end of data reading vint size")
    first = data[pos]
    if first == 0:
        raise ValueError(f"invalid EBML vint at offset {pos}")

    if   first & 0x80: width, mask = 1, 0x7F
    elif first & 0x40: width, mask = 2, 0x3F
    elif first & 0x20: width, mask = 3, 0x1F
    elif first & 0x10: width, mask = 4, 0x0F
    elif first & 0x08: width, mask = 5, 0x07
    elif first & 0x04: width, mask = 6, 0x03
    elif first & 0x02: width, mask = 7, 0x01
    else:               width, mask = 8, 0x00

    if pos + width > len(data):
        raise ValueError("truncated EBML vint")

    value = first & mask
    for i in range(1, width):
        value = (value << 8) | data[pos + i]

    # All-1s = unknown size.
    unknown = (mask << (8 * (width - 1))) | ((1 << (8 * (width - 1))) - 1)
    if value == unknown:
        return -1, width
    return value, width


def _read_uint(data: bytes, pos: int, size: int) -> int:
    """Read a big-endian unsigned integer of *size* bytes."""
    value = 0
    for i in range(size):
        value = (value << 8) | data[pos + i]
    return value


def _read_string(data: bytes, pos: int, size: int) -> str:
    """Read a UTF-8/ASCII string, stripping trailing NULs."""
    return data[pos:pos + size].rstrip(b"\x00").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Element iteration
# ---------------------------------------------------------------------------

def _iter_elements(data: bytes, pos: int, end: int):
    """Yield (element_id, data_offset, data_size) for elements in [pos, end)."""
    while pos < end:
        try:
            eid, id_len = _read_element_id(data, pos)
            size, size_len = _read_vint_size(data, pos + id_len)
        except ValueError:
            break
        data_offset = pos + id_len + size_len
        if size < 0:
            # Unknown size — cannot iterate further.
            break
        yield eid, data_offset, size
        pos = data_offset + size


# ---------------------------------------------------------------------------
# File-level readers (seek + read chunks, parse in memory)
# ---------------------------------------------------------------------------

def _read_chunk(f, offset: int, size: int) -> bytes:
    """Seek to *offset* and read *size* bytes."""
    f.seek(offset)
    return f.read(size)


def _find_segment(f) -> tuple[int, int]:
    """Locate the Segment element.  Returns (data_offset, data_size).

    data_size may be -1 for unknown/streaming segments.
    """
    # Read enough for the EBML header + start of Segment.
    # EBML header is typically <40 bytes; Segment header is ~12 bytes.
    header = _read_chunk(f, 0, 1024)

    pos = 0
    # Skip EBML header element.
    eid, id_len = _read_element_id(header, pos)
    size, size_len = _read_vint_size(header, pos + id_len)
    pos += id_len + size_len + size  # skip past EBML header data

    # Next element should be Segment.
    eid, id_len = _read_element_id(header, pos)
    size, size_len = _read_vint_size(header, pos + id_len)
    if eid != _ID_SEGMENT:
        raise ValueError(f"expected Segment element, got 0x{eid:X}")
    data_offset = pos + id_len + size_len
    return data_offset, size


def _parse_seekhead(f, seg_data_offset: int) -> dict[int, int]:
    """Parse the SeekHead to find absolute positions of key elements.

    Returns {element_id: absolute_file_offset}.
    """
    # SeekHead is the first child of Segment — read a generous chunk.
    chunk = _read_chunk(f, seg_data_offset, 4096)

    result = {}
    for eid, doff, dsize in _iter_elements(chunk, 0, len(chunk)):
        if eid == _ID_SEEKHEAD:
            for seek_eid, seek_doff, seek_dsize in _iter_elements(chunk, doff, doff + dsize):
                if seek_eid == _ID_SEEK:
                    seek_id = None
                    seek_pos = None
                    for child_eid, child_doff, child_dsize in _iter_elements(chunk, seek_doff, seek_doff + seek_dsize):
                        if child_eid == _ID_SEEKID:
                            # SeekID is stored as a binary element ID.
                            seek_id, _ = _read_element_id(chunk, child_doff)
                        elif child_eid == _ID_SEEKPOSITION:
                            seek_pos = _read_uint(chunk, child_doff, child_dsize)
                    if seek_id is not None and seek_pos is not None:
                        result[seek_id] = seg_data_offset + seek_pos
            break  # only need the first SeekHead
    return result


def _parse_info(f, info_pos: int) -> int:
    """Read TimestampScale from the Info element.  Returns nanoseconds per
    Matroska timestamp unit (default 1_000_000 = 1 ms).
    """
    # Info element is small — 256 bytes is plenty.
    raw = _read_chunk(f, info_pos, 512)
    eid, id_len = _read_element_id(raw, 0)
    size, size_len = _read_vint_size(raw, id_len)
    data_start = id_len + size_len
    # Ensure we have enough data.
    need = data_start + size
    if need > len(raw):
        raw = _read_chunk(f, info_pos, need)

    for child_eid, child_doff, child_dsize in _iter_elements(raw, data_start, data_start + size):
        if child_eid == _ID_TIMESTAMP_SCALE:
            return _read_uint(raw, child_doff, child_dsize)
    return 1_000_000  # default


def _parse_track_compression(raw: bytes, enc_doff: int, enc_dsize: int):
    """Parse a ContentEncoding element.  Returns (algo, settings) or None.

    *algo* is 0 (zlib) or 3 (header stripping), with associated
    *settings* (header bytes for stripping, or None).
    """
    for comp_eid, comp_doff, comp_dsize in _iter_elements(raw, enc_doff, enc_doff + enc_dsize):
        if comp_eid != _ID_CONTENTCOMPRESSION:
            continue
        # ContentCompAlgo defaults to 0 (zlib) when omitted.
        algo = _COMP_ZLIB
        settings = None
        for cc_eid, cc_doff, cc_dsize in _iter_elements(raw, comp_doff, comp_doff + comp_dsize):
            if cc_eid == _ID_CONTENTCOMPALGO:
                algo = _read_uint(raw, cc_doff, cc_dsize)
            elif cc_eid == _ID_CONTENTCOMPSETTINGS:
                settings = raw[cc_doff:cc_doff + cc_dsize]
        return algo, settings
    return None


def _parse_tracks(f, tracks_pos: int) -> list[tuple[int, str, object]]:
    """Parse the Tracks element.

    Returns an ordered list of (track_number, codec_id, compression)
    for every TrackEntry, in the order they appear (which matches
    FFmpeg's stream index assignment).

    *compression* is ``None`` (no compression), ``("zlib",)`` for zlib,
    or ``("header_strip", settings_bytes)`` for header stripping.
    """
    # Tracks can be a few KB.  Read the element header first to get size.
    hdr = _read_chunk(f, tracks_pos, 12)
    eid, id_len = _read_element_id(hdr, 0)
    size, size_len = _read_vint_size(hdr, id_len)
    data_start = id_len + size_len
    raw = _read_chunk(f, tracks_pos, data_start + size)

    entries = []
    for child_eid, child_doff, child_dsize in _iter_elements(raw, data_start, data_start + size):
        if child_eid != _ID_TRACKENTRY:
            continue
        track_number = None
        codec_id = ""
        compression = None
        for te_eid, te_doff, te_dsize in _iter_elements(raw, child_doff, child_doff + child_dsize):
            if te_eid == _ID_TRACKNUMBER:
                track_number = _read_uint(raw, te_doff, te_dsize)
            elif te_eid == _ID_CODECID:
                codec_id = _read_string(raw, te_doff, te_dsize)
            elif te_eid == _ID_CONTENTENCODINGS:
                for enc_eid, enc_doff, enc_dsize in _iter_elements(raw, te_doff, te_doff + te_dsize):
                    if enc_eid == _ID_CONTENTENCODING:
                        result = _parse_track_compression(raw, enc_doff, enc_dsize)
                        if result is not None:
                            algo, settings = result
                            if algo == _COMP_ZLIB:
                                compression = ("zlib",)
                            elif algo == _COMP_HEADER_STRIP:
                                compression = ("header_strip", settings or b"")
        if track_number is not None:
            entries.append((track_number, codec_id, compression))
    return entries


def _parse_cues(f, cues_pos: int, target_track_nums: set[int],
                seg_data_offset: int, file_size: int):
    """Parse the Cues element for clusters referencing target tracks.

    Returns ``(cluster_entries, block_entries)`` where:

    *cluster_entries* — sorted, deduplicated list of
    ``(cue_time, cluster_abs_pos)`` for every cluster containing at
    least one target-track block.  Used as the fallback path when
    ``CueRelativePosition`` is absent.

    *block_entries* — list of
    ``(cue_time, cluster_abs_pos, rel_pos, track_num)`` giving per-block
    direct offsets, or ``None`` if any target CueTrackPositions entry
    lacks ``CueRelativePosition``.  When present this enables the fast
    direct-read path that skips cluster-level parsing entirely.
    """
    # Read the Cues element header to get its size.
    hdr = _read_chunk(f, cues_pos, 12)
    eid, id_len = _read_element_id(hdr, 0)
    size, size_len = _read_vint_size(hdr, id_len)
    data_start = id_len + size_len

    # Cues can be 10-800 KB.  Read the whole thing.
    total = data_start + size
    if size < 0:
        total = file_size - cues_pos
    raw = _read_chunk(f, cues_pos, total)

    cluster_results = []
    seen_positions = set()
    block_results = []
    has_all_relpos = True

    cue_end = min(data_start + size, len(raw)) if size >= 0 else len(raw)
    for cp_eid, cp_doff, cp_dsize in _iter_elements(raw, data_start, cue_end):
        if cp_eid != _ID_CUEPOINT:
            continue
        cue_time = None
        cluster_pos = None
        matched = False
        for child_eid, child_doff, child_dsize in _iter_elements(raw, cp_doff, cp_doff + cp_dsize):
            if child_eid == _ID_CUETIME:
                cue_time = _read_uint(raw, child_doff, child_dsize)
            elif child_eid == _ID_CUETRACKPOS:
                cue_track = None
                cue_cluster = None
                cue_relpos = None
                for tp_eid, tp_doff, tp_dsize in _iter_elements(raw, child_doff, child_doff + child_dsize):
                    if tp_eid == _ID_CUETRACK:
                        cue_track = _read_uint(raw, tp_doff, tp_dsize)
                    elif tp_eid == _ID_CUECLUSTERPOS:
                        cue_cluster = _read_uint(raw, tp_doff, tp_dsize)
                    elif tp_eid == _ID_CUERELPOS:
                        cue_relpos = _read_uint(raw, tp_doff, tp_dsize)
                if cue_track in target_track_nums and cue_cluster is not None:
                    cluster_pos = seg_data_offset + cue_cluster
                    matched = True
                    if cue_relpos is not None:
                        block_results.append(
                            (cue_time, cluster_pos, cue_relpos, cue_track))
                    else:
                        has_all_relpos = False
        if matched and cluster_pos is not None and cluster_pos not in seen_positions:
            seen_positions.add(cluster_pos)
            cluster_results.append((cue_time, cluster_pos))

    cluster_results.sort(key=lambda x: x[1])
    return cluster_results, (block_results if has_all_relpos else None)


# ---------------------------------------------------------------------------
# Cluster parsing — extract subtitle blocks
# ---------------------------------------------------------------------------

def _read_block_header(data: bytes, pos: int) -> tuple[int, int, int, int]:
    """Parse the track-number vint + 2-byte relative timestamp + flags byte
    at the start of a SimpleBlock / Block payload.

    Returns (track_number, rel_timestamp_ms, header_size).
    """
    track_num, vint_len = _read_vint_size(data, pos)
    rel_ts = struct.unpack(">h", data[pos + vint_len:pos + vint_len + 2])[0]
    flags = data[pos + vint_len + 2]
    return track_num, rel_ts, vint_len + 3, flags


_CLUSTER_PREFETCH = 65536  # 64 KB initial read per cluster


def _parse_cluster_blocks(f, cluster_pos: int, target_track_nums: set[int],
                          ts_scale_ns: int) -> list[tuple[int, int, bytes]]:
    """Extract subtitle blocks from a single cluster.

    Reads the cluster element, iterates its children, and returns blocks
    for matching tracks as (track_number, abs_timestamp_90khz, payload).

    Uses a sliding-window buffer: ``buf`` holds a chunk of data starting
    at file offset ``cluster_pos + buf_origin``.  When the current
    element offset ``off`` leaves the window, a new chunk is read at
    ``off``.  Non-matching blocks (video/audio) are skipped by jumping
    ``off`` past them without reading their data.
    """
    # --- Initial read: cluster element header + first chunk. ---
    buf = _read_chunk(f, cluster_pos, _CLUSTER_PREFETCH)
    buf_origin = 0  # cluster-relative offset of buf[0]
    if len(buf) < 12:
        return []
    try:
        eid, id_len = _read_element_id(buf, 0)
        esize, size_len = _read_vint_size(buf, id_len)
    except ValueError:
        return []
    if eid != _ID_CLUSTER:
        return []

    hdr_size = id_len + size_len
    cluster_data_size = esize if esize >= 0 else None
    end = (hdr_size + cluster_data_size) if cluster_data_size is not None else None

    cluster_ts = 0
    results = []
    off = hdr_size  # cluster-relative offset of current element

    def _ensure_buf(need_off, need_len):
        """Make sure buf covers [need_off, need_off+need_len).

        Reads a new _CLUSTER_PREFETCH-sized window when the current
        buffer doesn't cover the requested range.  Returns True if the
        data is available, False otherwise.
        """
        nonlocal buf, buf_origin
        rel = need_off - buf_origin
        if 0 <= rel and rel + need_len <= len(buf):
            return True
        # Read a new window starting at need_off.
        buf = _read_chunk(f, cluster_pos + need_off, _CLUSTER_PREFETCH)
        buf_origin = need_off
        return len(buf) >= need_len

    def _buf_slice(abs_off, length):
        """Return bytes at cluster-relative [abs_off, abs_off+length)."""
        return buf[abs_off - buf_origin: abs_off - buf_origin + length]

    while end is None or off < end:
        # --- Read element header (ID + size). ---
        if not _ensure_buf(off, 12):
            break
        boff = off - buf_origin
        try:
            eid, id_len = _read_element_id(buf, boff)
            esize, size_len = _read_vint_size(buf, boff + id_len)
        except (ValueError, IndexError):
            break
        if esize < 0:
            break

        data_off = off + id_len + size_len   # cluster-relative
        next_off = data_off + esize

        if eid == _ID_CLUSTER_TS:
            if _ensure_buf(data_off, esize):
                cluster_ts = _read_uint(buf, data_off - buf_origin, esize)

        elif eid in (_ID_SIMPLEBLOCK, _ID_BLOCK):
            # Read block header (track vint + 2-byte rel_ts + flags).
            if not _ensure_buf(data_off, min(8, esize)):
                off = next_off
                continue
            try:
                track_num, rel_ts, blk_hdr_size, flags = _read_block_header(
                    buf, data_off - buf_origin)
            except (ValueError, struct.error, IndexError):
                off = next_off
                continue

            if track_num in target_track_nums:
                lacing = (flags >> 1) & 0x03
                if lacing == 0:
                    payload_off = data_off + blk_hdr_size
                    payload_size = esize - blk_hdr_size
                    if _ensure_buf(payload_off, payload_size):
                        payload = bytes(_buf_slice(payload_off, payload_size))
                    else:
                        payload = _read_chunk(f, cluster_pos + payload_off,
                                              payload_size)
                    abs_ts_matroska = cluster_ts + rel_ts
                    abs_ts_ms = abs_ts_matroska * ts_scale_ns / 1_000_000
                    abs_ts_90khz = int(abs_ts_ms * 90)
                    results.append((track_num, abs_ts_90khz, payload))

        elif eid == _ID_BLOCKGROUP:
            # BlockGroup: read its data and look for a Block child.
            if _ensure_buf(data_off, esize):
                grp_data = bytes(_buf_slice(data_off, esize))
            else:
                grp_data = _read_chunk(f, cluster_pos + data_off, esize)
            for grp_eid, grp_doff, grp_dsize in _iter_elements(
                    grp_data, 0, esize):
                if grp_eid == _ID_BLOCK:
                    try:
                        track_num, rel_ts, blk_hdr_size, flags = \
                            _read_block_header(grp_data, grp_doff)
                    except (ValueError, struct.error):
                        continue
                    if track_num in target_track_nums:
                        lacing = (flags >> 1) & 0x03
                        if lacing == 0:
                            payload = bytes(grp_data[
                                grp_doff + blk_hdr_size:grp_doff + grp_dsize])
                            abs_ts_matroska = cluster_ts + rel_ts
                            abs_ts_ms = abs_ts_matroska * ts_scale_ns / 1_000_000
                            abs_ts_90khz = int(abs_ts_ms * 90)
                            results.append((track_num, abs_ts_90khz, payload))

        off = next_off

    return results


# ---------------------------------------------------------------------------
# .sup reconstruction
# ---------------------------------------------------------------------------

def _block_payload_to_sup(pts_90khz: int, payload: bytes) -> bytes:
    """Wrap a raw MKV PGS block payload in .sup segment framing.

    Each MKV block for S_HDMV/PGS contains one or more raw PGS segments
    (seg_type + seg_size + data) without the .sup "PG" + PTS/DTS header.
    This function prepends the header to each segment.
    """
    pts_bytes = struct.pack(">I", pts_90khz & 0xFFFFFFFF)
    dts_bytes = b"\x00\x00\x00\x00"
    prefix = b"PG" + pts_bytes + dts_bytes

    # A single block may contain multiple PGS segments back-to-back.
    result = bytearray()
    pos = 0
    while pos < len(payload):
        if pos + 3 > len(payload):
            break
        seg_type = payload[pos]
        seg_size = struct.unpack(">H", payload[pos + 1:pos + 3])[0]
        seg_end = pos + 3 + seg_size
        if seg_end > len(payload):
            # Truncated segment — include what we have.
            seg_end = len(payload)
        # .sup segment: PG(2) + PTS(4) + DTS(4) + seg_type(1) + seg_size(2) + data
        result += prefix
        result += payload[pos:seg_end]
        pos = seg_end

    return bytes(result)


# ---------------------------------------------------------------------------
# Parallel cluster reading
# ---------------------------------------------------------------------------

_PARALLEL_WORKERS = 16


def _get_thread_fh(input_path: str):
    """Return a thread-local file handle for *input_path*."""
    tls = _get_thread_fh._tls
    key = f"f_{id(threading.current_thread())}"
    f = getattr(tls, key, None)
    if f is None or f.closed:
        f = open(input_path, "rb", buffering=262144)
        setattr(tls, key, f)
    return f


_get_thread_fh._tls = threading.local()


def _read_cluster_worker(args):
    """Worker function for parallel cluster reading.

    Opens its own file handle (avoids seek contention between threads),
    parses one cluster, decompresses matching blocks, and returns a list
    of ``(track_num, pts_90khz, decompressed_payload)`` tuples.
    """
    input_path, cluster_pos, target_track_nums, ts_scale_ns, track_compression = args
    f = _get_thread_fh(input_path)

    blocks = _parse_cluster_blocks(f, cluster_pos, target_track_nums, ts_scale_ns)
    return [
        (tn, pts, _decompress_payload(payload, track_compression.get(tn)))
        for tn, pts, payload in blocks
    ]


def _read_cluster_group_direct(args):
    """Read subtitle blocks for one cluster using CueRelativePosition.

    *args* = ``(input_path, cluster_pos, block_entries, ts_scale_ns,
    track_compression)`` where *block_entries* is a list of
    ``(cue_time, rel_pos, track_num)`` for blocks in this cluster.

    Strategy:
      1. Read 24 bytes at cluster_pos to determine the cluster element
         header size (needed to convert rel_pos to absolute offset).
      2. Read each subtitle block individually at its exact position.
         Each PGS block is tiny (~0.5-2 KB) so reading them directly
         is very fast.  Unlike the region-read approach this avoids
         pulling in audio data interleaved between subtitle blocks.

    Returns list of ``(track_num, pts_90khz, decompressed_payload)``.
    """
    input_path, cluster_pos, block_entries, ts_scale_ns, track_compression = args
    f = _get_thread_fh(input_path)

    # Step 1: determine cluster header size.
    hdr_raw = _read_chunk(f, cluster_pos, 24)
    if len(hdr_raw) < 8:
        return []
    try:
        eid, id_len = _read_element_id(hdr_raw, 0)
        esize, size_len = _read_vint_size(hdr_raw, id_len)
    except ValueError:
        return []
    if eid != _ID_CLUSTER:
        return []
    cluster_hdr_size = id_len + size_len  # typically 8

    # Step 2: read each block at its exact position.
    data_base = cluster_pos + cluster_hdr_size
    results = []
    for cue_time, rel_pos, track_num in block_entries:
        block_pos = data_base + rel_pos
        # Read element header first (12 bytes covers ID + size vint).
        blk_hdr = _read_chunk(f, block_pos, 12)
        if len(blk_hdr) < 4:
            continue
        try:
            blk_eid, blk_id_len = _read_element_id(blk_hdr, 0)
            blk_esize, blk_size_len = _read_vint_size(blk_hdr, blk_id_len)
        except (ValueError, IndexError):
            continue
        if blk_esize < 0:
            continue
        elem_hdr_size = blk_id_len + blk_size_len

        # Read the full element data (block header + payload).
        blk_data = _read_chunk(f, block_pos + elem_hdr_size, blk_esize)
        if len(blk_data) < 4:
            continue

        if blk_eid == _ID_SIMPLEBLOCK:
            payload_info = _extract_block_payload(
                blk_data, 0, blk_esize, cue_time, ts_scale_ns)
            if payload_info is not None:
                pts, payload = payload_info
                payload = _decompress_payload(
                    payload, track_compression.get(track_num))
                results.append((track_num, pts, payload))
        elif blk_eid == _ID_BLOCKGROUP:
            for grp_eid, grp_doff, grp_dsize in _iter_elements(
                    blk_data, 0, blk_esize):
                if grp_eid == _ID_BLOCK:
                    payload_info = _extract_block_payload(
                        blk_data, grp_doff, grp_dsize,
                        cue_time, ts_scale_ns)
                    if payload_info is not None:
                        pts, payload = payload_info
                        payload = _decompress_payload(
                            payload, track_compression.get(track_num))
                        results.append((track_num, pts, payload))

    return results


def _extract_block_payload(data, data_off, data_size, cue_time, ts_scale_ns):
    """Parse block header and return ``(pts_90khz, payload)`` or None."""
    if data_off + 4 > len(data):
        return None
    try:
        track_num, rel_ts, blk_hdr_size, flags = _read_block_header(data, data_off)
    except (ValueError, struct.error, IndexError):
        return None
    lacing = (flags >> 1) & 0x03
    if lacing != 0:
        return None
    payload_off = data_off + blk_hdr_size
    payload_size = data_size - blk_hdr_size
    if payload_off + payload_size > len(data):
        return None
    payload = bytes(data[payload_off:payload_off + payload_size])
    abs_ts_matroska = cue_time + rel_ts
    abs_ts_ms = abs_ts_matroska * ts_scale_ns / 1_000_000
    pts_90khz = int(abs_ts_ms * 90)
    return pts_90khz, payload


# ---------------------------------------------------------------------------
# Shared metadata parsing
# ---------------------------------------------------------------------------

def _decompress_payload(payload: bytes, compression) -> bytes:
    """Decompress a block payload according to the track's compression.

    *compression* is None (no-op), ``("zlib",)``, or
    ``("header_strip", settings_bytes)``.
    """
    if compression is None:
        return payload
    if compression[0] == "zlib":
        return zlib.decompress(payload)
    if compression[0] == "header_strip":
        return compression[1] + payload
    return payload


def _parse_mkv_metadata(f, file_size: int, tracks: list):
    """Parse MKV structure and build track mappings.

    Returns ``(seg_data_offset, ts_scale_ns, target_mkv_nums,
    mkv_num_to_enum, cluster_entries, block_entries,
    track_compression)`` or ``None`` on any structural problem.

    *block_entries* is a list of per-block direct entries
    ``(cue_time, cluster_abs_pos, rel_pos, track_num)`` when
    ``CueRelativePosition`` is available, or ``None`` if not.

    *tracks* is the list from ``probe_pgs_tracks()`` — each dict must
    have an ``"index"`` key (FFmpeg stream index).

    *track_compression* maps MKV track number → compression tuple.
    """
    try:
        seg_data_offset, seg_data_size = _find_segment(f)
    except (ValueError, IndexError):
        return None

    positions = _parse_seekhead(f, seg_data_offset)
    if _ID_CUES not in positions or _ID_TRACKS not in positions:
        return None

    ts_scale_ns = (
        _parse_info(f, positions[_ID_INFO])
        if _ID_INFO in positions else 1_000_000
    )

    all_tracks = _parse_tracks(f, positions[_ID_TRACKS])
    ffmpeg_idx_to_mkv_num = {i: tn for i, (tn, _, _) in enumerate(all_tracks)}
    track_compression = {tn: comp for tn, _, comp in all_tracks}

    target_mkv_nums = set()
    mkv_num_to_enum = {}
    for ti, track in enumerate(tracks):
        mkv_num = ffmpeg_idx_to_mkv_num.get(track["index"])
        if mkv_num is None:
            return None
        target_mkv_nums.add(mkv_num)
        mkv_num_to_enum[mkv_num] = ti

    if not target_mkv_nums:
        return None

    cluster_entries, block_entries = _parse_cues(
        f, positions[_ID_CUES], target_mkv_nums,
        seg_data_offset, file_size,
    )
    if not cluster_entries:
        # Subtitle tracks not indexed in Cues (common — many muxers only
        # index video).  Fall back to ALL cluster positions from whatever
        # tracks ARE indexed, then scan those clusters for subtitle blocks.
        all_track_nums = {tn for tn, _, _ in all_tracks}
        cluster_entries, _ = _parse_cues(
            f, positions[_ID_CUES], all_track_nums,
            seg_data_offset, file_size,
        )
        # block_entries stays None → callers use cluster-scan path.
        block_entries = None
        if not cluster_entries:
            return None

    return (seg_data_offset, ts_scale_ns, target_mkv_nums,
            mkv_num_to_enum, cluster_entries, block_entries,
            track_compression)


def probe_mkv_subtitle_cues(input_path: str, tracks: list) -> bool:
    """Quick check: does this MKV have subtitle tracks indexed in its Cues?

    Returns ``True`` when the Cues element contains entries for at least
    one of the requested PGS tracks — meaning the fast per-block extraction
    path is available.  Returns ``False`` if Cues only index video/audio
    (requiring the slower cluster-scan fallback), or on any parse error.

    This is lightweight (~100 ms on NAS): reads only the EBML header,
    SeekHead, Tracks element, and Cues element — no cluster data.
    """
    try:
        file_size = os.path.getsize(input_path)
        with open(input_path, "rb", buffering=262144) as f:
            seg_data_offset, _ = _find_segment(f)
            positions = _parse_seekhead(f, seg_data_offset)
            if _ID_CUES not in positions or _ID_TRACKS not in positions:
                return False

            all_tracks = _parse_tracks(f, positions[_ID_TRACKS])
            ffmpeg_idx_to_mkv_num = {
                i: tn for i, (tn, _, _) in enumerate(all_tracks)
            }
            target_mkv_nums = set()
            for t in tracks:
                mkv_num = ffmpeg_idx_to_mkv_num.get(t["index"])
                if mkv_num is not None:
                    target_mkv_nums.add(mkv_num)
            if not target_mkv_nums:
                return False

            cluster_entries, _ = _parse_cues(
                f, positions[_ID_CUES], target_mkv_nums,
                seg_data_offset, file_size,
            )
            return len(cluster_entries) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------

def _group_blocks_by_cluster(block_entries):
    """Group block entries by cluster position.

    Returns dict mapping cluster_pos → list of (cue_time, rel_pos, track_num).
    """
    groups = {}
    for cue_time, cluster_pos, rel_pos, track_num in block_entries:
        groups.setdefault(cluster_pos, []).append((cue_time, rel_pos, track_num))
    return groups


def extract_pgs_tracks_mkv(input_path: str, tracks: list, temp_dir: str,
                           **_kw) -> dict | None:
    """Extract ALL PGS display sets from an MKV via Cues-based direct I/O.

    *tracks* is the list of track dicts from ``probe_pgs_tracks()`` (each
    with an ``"index"`` key = FFmpeg stream index).

    Returns a dict mapping enumeration index → temp ``.sup`` file path
    (same contract as ``extract_all_pgs_tracks``), or ``None`` if Cues
    are not available or a parse error occurs (caller falls back to FFmpeg).
    """
    file_size = os.path.getsize(input_path)

    with open(input_path, "rb", buffering=262144) as f:
        meta = _parse_mkv_metadata(f, file_size, tracks)
        if meta is None:
            return None
        (seg_data_offset, ts_scale_ns, target_mkv_nums,
         mkv_num_to_enum, cluster_entries, block_entries,
         track_compression) = meta

    sup_buffers: dict[int, bytearray] = {ti: bytearray() for ti in range(len(tracks))}

    if block_entries is not None:
        # Fast path — CueRelativePosition available.  Two reads per
        # cluster (header + subtitle region) instead of scanning the
        # entire cluster.
        groups = _group_blocks_by_cluster(block_entries)
        total_groups = len(groups)
        print(f"{info('Extracting')} {len(tracks)} PGS track(s) via MKV index "
              f"({total_groups} clusters, {len(block_entries)} blocks)...")

        # Sort by file position for sequential I/O.
        sorted_clusters = sorted(groups.items(), key=lambda kv: kv[0])
        work_args = [
            (input_path, cpos, entries, ts_scale_ns, track_compression)
            for cpos, entries in sorted_clusters
        ]

        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
            futures = [pool.submit(_read_cluster_group_direct, a)
                       for a in work_args]
            for ci, fut in enumerate(futures):
                if ci % 100 == 0 or ci == total_groups - 1:
                    pct = ((ci + 1) / total_groups) * 100
                    print(f"\r  Progress: {pct:5.1f}%  "
                          f"({ci + 1}/{total_groups} clusters)",
                          end="", flush=True)
                blocks = fut.result()
                for track_num, pts_90khz, payload in blocks:
                    enum_idx = mkv_num_to_enum.get(track_num)
                    if enum_idx is not None:
                        sup_buffers[enum_idx] += _block_payload_to_sup(
                            pts_90khz, payload)

        print(f"\r  Progress: 100.0%  "
              f"({total_groups}/{total_groups} clusters)")
    else:
        # Fallback — scan clusters sequentially (no CueRelativePosition).
        total_clusters = len(cluster_entries)
        print(f"{info('Extracting')} {len(tracks)} PGS track(s) via MKV index "
              f"({total_clusters} clusters)...")

        work_args = [
            (input_path, cp, target_mkv_nums, ts_scale_ns, track_compression)
            for _, cp in cluster_entries
        ]

        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
            futures = [pool.submit(_read_cluster_worker, a) for a in work_args]
            for ci, fut in enumerate(futures):
                if ci % 50 == 0 or ci == total_clusters - 1:
                    pct = ((ci + 1) / total_clusters) * 100
                    print(f"\r  Progress: {pct:5.1f}%  "
                          f"({ci + 1}/{total_clusters} clusters)",
                          end="", flush=True)
                blocks = fut.result()
                for track_num, pts_90khz, payload in blocks:
                    enum_idx = mkv_num_to_enum.get(track_num)
                    if enum_idx is not None:
                        sup_buffers[enum_idx] += _block_payload_to_sup(
                            pts_90khz, payload)

        print(f"\r  Progress: 100.0%  "
              f"({total_clusters}/{total_clusters} clusters)")

    sup_paths = {}
    for ti in range(len(tracks)):
        sup_path = os.path.join(temp_dir, f"track_{ti}.sup")
        with open(sup_path, "wb") as out:
            out.write(sup_buffers[ti])
        sup_paths[ti] = sup_path

    return sup_paths


def _read_single_block_direct(args):
    """Read a single subtitle block at its exact file position.

    *args* = ``(input_path, cluster_pos, cue_time, rel_pos, track_num,
    ts_scale_ns, track_compression)``.

    Returns ``(track_num, pts_90khz, decompressed_payload)`` or ``None``.
    """
    (input_path, cluster_pos, cue_time, rel_pos, track_num,
     ts_scale_ns, track_compression) = args
    f = _get_thread_fh(input_path)

    # Determine cluster header size (cache in thread-local per cluster).
    tls = _get_thread_fh._tls
    cache = getattr(tls, "hdr_cache", None)
    if cache is None:
        cache = {}
        tls.hdr_cache = cache

    hdr_size = cache.get(cluster_pos)
    if hdr_size is None:
        hdr_raw = _read_chunk(f, cluster_pos, 12)
        if len(hdr_raw) < 5:
            return None
        try:
            _, id_len = _read_element_id(hdr_raw, 0)
            _, size_len = _read_vint_size(hdr_raw, id_len)
        except ValueError:
            return None
        hdr_size = id_len + size_len
        cache[cluster_pos] = hdr_size

    block_pos = cluster_pos + hdr_size + rel_pos

    # Read element header.
    blk_hdr = _read_chunk(f, block_pos, 12)
    if len(blk_hdr) < 4:
        return None
    try:
        blk_eid, blk_id_len = _read_element_id(blk_hdr, 0)
        blk_esize, blk_size_len = _read_vint_size(blk_hdr, blk_id_len)
    except (ValueError, IndexError):
        return None
    if blk_esize < 0:
        return None
    elem_hdr_size = blk_id_len + blk_size_len

    # Read block data.
    blk_data = _read_chunk(f, block_pos + elem_hdr_size, blk_esize)
    if len(blk_data) < 4:
        return None

    if blk_eid == _ID_SIMPLEBLOCK:
        info = _extract_block_payload(blk_data, 0, blk_esize,
                                      cue_time, ts_scale_ns)
        if info is None:
            return None
        pts, payload = info
        payload = _decompress_payload(payload,
                                      track_compression.get(track_num))
        return (track_num, pts, payload)

    elif blk_eid == _ID_BLOCKGROUP:
        for grp_eid, grp_doff, grp_dsize in _iter_elements(
                blk_data, 0, blk_esize):
            if grp_eid == _ID_BLOCK:
                info = _extract_block_payload(blk_data, grp_doff, grp_dsize,
                                              cue_time, ts_scale_ns)
                if info is None:
                    continue
                pts, payload = info
                payload = _decompress_payload(
                    payload, track_compression.get(track_num))
                return (track_num, pts, payload)

    return None


def extract_analysis_samples_mkv(input_path: str, tracks: list,
                                 temp_dir: str,
                                 max_ds: int = 125,
                                 ready_check=None,
                                 deadline: float = None) -> list | None:
    """Extract analysis samples from an MKV via Cues-based direct I/O.

    Same contract as ``extract_analysis_samples()`` — returns a list of
    temp ``.sup`` file paths (index-aligned with *tracks*), or ``None``
    to signal the caller to fall back to FFmpeg.

    When ``CueRelativePosition`` is available, uses **per-track
    targeting**: for each track, reads only the blocks belonging to that
    track until it has enough display sets.  This is dramatically faster
    for sparse tracks — a track with 4 blocks needs only 4 reads instead
    of scanning thousands of clusters.

    Falls back to cluster-by-cluster scanning when CueRelativePosition
    is absent.

    *max_ds*: stop after collecting this many display sets per track.
    *ready_check*: optional callback ``(list[str]) -> bool``.  Called
        periodically with the current temp ``.sup`` file paths.  If it
        returns True (e.g. all tracks have conclusive detection), extraction
        stops early — mirroring the FFmpeg content-based watchdog.
    *deadline*: absolute ``time.monotonic()`` timestamp.  When set, the
        cluster-scan path stops when the deadline expires.  Ignored on the
        fast path (per-block reads are already quick).
    """
    file_size = os.path.getsize(input_path)

    with open(input_path, "rb", buffering=262144) as f:
        meta = _parse_mkv_metadata(f, file_size, tracks)
        if meta is None:
            return None
        (seg_data_offset, ts_scale_ns, target_mkv_nums,
         mkv_num_to_enum, cluster_entries, block_entries,
         track_compression) = meta

    # --- Prepare temp paths ---
    sup_paths = []
    for ti in range(len(tracks)):
        sup_paths.append(os.path.join(temp_dir, f"track_{ti}.sup"))

    from .constants import SEG_END
    ds_counts: dict[int, int] = {ti: 0 for ti in range(len(tracks))}
    sup_buffers: dict[int, bytearray] = {ti: bytearray() for ti in range(len(tracks))}

    def _count_ds(payload):
        """Count *content* display sets in a raw PGS payload.

        Only display sets containing an ODS (Object Definition Segment)
        are counted — matching ``read_sup_streaming``'s behaviour so that
        ``max_ds=10`` yields 10 renderable subtitles, not 10 total DS
        (which would include clear/hide display sets).
        """
        from .constants import SEG_ODS
        count = 0
        has_ods = False
        pos = 0
        while pos < len(payload):
            if pos + 3 > len(payload):
                break
            seg_type = payload[pos]
            if seg_type == SEG_ODS:
                has_ods = True
            elif seg_type == SEG_END:
                if has_ods:
                    count += 1
                has_ods = False
            seg_size = struct.unpack(">H", payload[pos + 1:pos + 3])[0]
            pos += 3 + seg_size
        return count

    if block_entries is not None:
        # ===== Fast path: per-track targeted reads =====
        #
        # Build per-track block lists, sorted by time.  For each track
        # we read only as many blocks as needed for max_ds display sets.
        # Sparse tracks (4 blocks) finish almost instantly instead of
        # waiting for thousands of clusters to be scanned.

        # Invert mkv_num_to_enum for track_num → enum_idx lookup.
        enum_to_mkv = {v: k for k, v in mkv_num_to_enum.items()}

        per_track_blocks: dict[int, list] = {ti: [] for ti in range(len(tracks))}
        for cue_time, cluster_pos, rel_pos, track_num in block_entries:
            enum_idx = mkv_num_to_enum.get(track_num)
            if enum_idx is not None:
                per_track_blocks[enum_idx].append(
                    (cue_time, cluster_pos, rel_pos, track_num))

        # Sort each track's blocks by time.
        for ti in per_track_blocks:
            per_track_blocks[ti].sort(key=lambda e: e[0])

        # Build the work queue: interleave blocks from all tracks so
        # the thread pool works on multiple tracks concurrently.  Stop
        # adding blocks for a track once we've queued enough (estimate
        # ~2 blocks per content DS pair as a conservative cap, actual
        # counting happens as results come in).
        max_blocks_per_track = max_ds * 5  # generous cap
        work_items = []  # (enum_idx, work_args)
        for ti in range(len(tracks)):
            for bi, (ct, cp, rp, tn) in enumerate(per_track_blocks[ti]):
                if bi >= max_blocks_per_track:
                    break
                work_items.append((ti, (
                    input_path, cp, ct, rp, tn,
                    ts_scale_ns, track_compression,
                )))

        # Sort by file position for best sequential I/O pattern.
        work_items.sort(key=lambda item: (item[1][1], item[1][3]))

        _FLUSH_INTERVAL = 50
        done = False

        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
            # Submit in batches to allow early cancellation.
            futures = []
            for wi_idx, (ti, args) in enumerate(work_items):
                futures.append((ti, pool.submit(_read_single_block_direct, args)))

            for fi, (ti, fut) in enumerate(futures):
                if done:
                    fut.cancel()
                    continue

                # Skip tracks that already have enough DS.
                if ds_counts[ti] >= max_ds:
                    fut.cancel()
                    continue

                result = fut.result()
                if result is None:
                    continue

                track_num, pts_90khz, payload = result
                sup_buffers[ti] += _block_payload_to_sup(pts_90khz, payload)
                ds_counts[ti] += _count_ds(payload)

                # Check if all tracks satisfied.
                if all(ds_counts[t] >= max_ds
                       or not per_track_blocks[t]
                       for t in range(len(tracks))):
                    done = True
                    continue

                # Periodic flush + ready_check.
                if ready_check is not None and fi > 0 and fi % _FLUSH_INTERVAL == 0:
                    for t in range(len(tracks)):
                        with open(sup_paths[t], "wb") as out:
                            out.write(sup_buffers[t])
                    if ready_check(sup_paths):
                        done = True
                        continue

    else:
        # ===== Fallback: cluster-by-cluster scan =====
        entries = cluster_entries
        if not entries:
            return None

        work_args = [
            (input_path, cp, target_mkv_nums, ts_scale_ns, track_compression)
            for _, cp in entries
        ]
        _FLUSH_INTERVAL = 25
        done = False

        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
            futures = [pool.submit(_read_cluster_worker, a)
                       for a in work_args]
            for ci, fut in enumerate(futures):
                if done:
                    fut.cancel()
                    continue
                if all(ds_counts[ti] >= max_ds for ti in range(len(tracks))):
                    done = True
                    fut.cancel()
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    done = True
                    fut.cancel()
                    continue
                if ready_check is not None and ci > 0 and ci % _FLUSH_INTERVAL == 0:
                    for ti in range(len(tracks)):
                        with open(sup_paths[ti], "wb") as out:
                            out.write(sup_buffers[ti])
                    if ready_check(sup_paths):
                        done = True
                        fut.cancel()
                        continue
                blocks = fut.result()
                for track_num, pts_90khz, payload in blocks:
                    enum_idx = mkv_num_to_enum.get(track_num)
                    if enum_idx is None:
                        continue
                    if ds_counts[enum_idx] >= max_ds:
                        continue
                    sup_buffers[enum_idx] += _block_payload_to_sup(
                        pts_90khz, payload)
                    ds_counts[enum_idx] += _count_ds(payload)

    # --- Final flush of temp .sup files ---
    for ti in range(len(tracks)):
        with open(sup_paths[ti], "wb") as out:
            out.write(sup_buffers[ti])

    return sup_paths
