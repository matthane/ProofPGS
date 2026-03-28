"""Color space detection for PGS subtitle streams (SDR vs HDR).

Analyzes raw YCbCr palette entries to determine whether a PGS stream was
mastered for SDR (BT.709 + BT.1886) or HDR (BT.2020 + PQ).

Only palette entries that are both visible (alpha >= 32) and actually
referenced by the display set's bitmap are analyzed.  PGS authoring tools
can define high-alpha palette entries that are never rendered — persistent
"ghost" entries that would poison detection if included.  When bitmap data
is unavailable, all visible entries are considered as a fallback.

Primary signal: **PQ plausibility test**.  For each bitmap-referenced
palette entry with Y > 50, decode YCbCr → R'G'B' using the BT.2020
matrix and take the highest channel value.  If this value exceeds the PQ
code value for ~1000 nits (~0.75), the PQ interpretation implies luminance
far too high for subtitle content → gamma-encoded SDR, not PQ-encoded HDR.

This test is particularly effective for colored text (gold, yellow, cyan)
where Y-value-only thresholds are ambiguous.  For example, SDR gold text
at Y=178 decodes to R'≈0.92 under BT.2020, giving ~4800 nits in PQ —
obviously SDR.  Genuine HDR text at 203 nits gives R'≈0.58 → ~200 nits.

Secondary signals: Y-value thresholds and achromatic entry analysis
handle cases where the PQ test is inconclusive.
"""

# --- BT.2020 YCbCr → R'G'B' matrix coefficients (limited-range normalised) ---
# R' = Yn + 1.4746 * Crn
# G' = Yn - 0.1645 * Cbn - 0.5713 * Crn
# B' = Yn + 1.8814 * Cbn
_BT2020_CR_R = 1.4746
_BT2020_CB_G = -0.1645
_BT2020_CR_G = -0.5713
_BT2020_CB_B = 1.8814

# PQ code value thresholds (0–1 normalised, not limited-range Y).
# Computed via PQ OETF:  1000 nits → 0.7518,  400 nits → 0.6375.
#
# _PQ_IMPLAUSIBLE: if the max PQ channel value across all visible palette
#   entries exceeds this after BT.2020 decode, the PQ interpretation
#   gives >1000 nits — far too bright for subtitle content.  Almost
#   certainly SDR.
# _PQ_PLAUSIBLE:   if the max value stays below this, the PQ
#   interpretation gives <400 nits — perfectly reasonable for HDR
#   subtitle content at or near reference white (203 nits).
_PQ_IMPLAUSIBLE = 0.75   # ~1000 nits
_PQ_PLAUSIBLE = 0.65     # ~400 nits

# Y-value thresholds (limited-range Y, 8-bit) — secondary signal.
_Y_SDR_HIGH = 210   # SDR yellow = 219, SDR white = 235
_Y_HDR_HIGH = 170   # HDR 1000 nits = 181, HDR ref white = 143
_Y_SDR_MED = 195    # medium-confidence boundary in ambiguous zone

# Achromatic tolerance: entries with Cb and Cr both within this distance
# of 128 are treated as white/gray/black (color-matrix-independent).
_ACHRO_TOL = 3

# Minimum Y to consider an entry meaningful (skip shadows/outlines).
_MIN_Y = 50

# Minimum alpha to consider a palette entry visible.  PGS fade-in frames
# define the full palette but with near-zero alpha (1–11) on most entries.
# These are functionally invisible and should not influence detection.
# 32 (~12.5% opacity) excludes anti-aliasing fringe and fade-in entries
# while keeping any genuinely rendered content.
_MIN_ALPHA = 32


def _bt2020_max_channel(y, cr, cb):
    """Decode limited-range YCbCr to BT.2020 R'G'B', return max channel.

    Returns the maximum of (R', G', B') after BT.2020 matrix decode.
    This value represents a PQ code if the content is HDR, or a
    gamma-encoded value if SDR.  In either case, values above ~0.75
    imply >1000 nit luminance under PQ — implausible for subtitles.
    """
    yn = (y - 16.0) / 219.0
    crn = (cr - 128.0) / 224.0
    cbn = (cb - 128.0) / 224.0
    rp = yn + _BT2020_CR_R * crn
    gp = yn + _BT2020_CB_G * cbn + _BT2020_CR_G * crn
    bp = yn + _BT2020_CB_B * cbn
    return max(rp, gp, bp)


def detect_from_palettes(display_sets: list) -> dict:
    """Analyze palette entries from parsed display sets.

    Iterates every PDS segment, collects Y values and PQ-plausibility
    metrics from bitmap-referenced visible entries (alpha >= 32), and
    applies a multi-signal detection algorithm.

    Args:
        display_sets: list of display sets (each a dict with keys
                      ``pts``, ``pts_ms``, ``composition``, ``palettes``,
                      ``objects``).

    Returns:
        dict with keys:
          verdict:          "hdr" | "sdr" | None
          confidence:       "high" | "medium" | "low"
          max_y:            int  (highest Y among visible entries, or 0)
          max_achromatic_y: int | None  (highest Y among achromatic entries)
          max_pq_channel:   float  (max PQ channel value across all visible
                                   entries)
          num_palettes:     int  (number of PDS segments analyzed)
    """
    max_y = 0
    max_achromatic_y = None
    max_pq_channel = 0.0
    num_palettes = 0
    has_bright = False  # at least one entry with Y > _MIN_Y

    for ds in display_sets:
        palette = ds.get("palettes", {})
        if not palette:
            continue
        num_palettes += 1

        # Collect palette entry IDs actually referenced by the bitmap.
        # PGS authoring tools can include high-alpha palette entries that
        # are never rendered (ghost entries).  Only bitmap-referenced
        # entries are trustworthy for detection.  If no bitmap data is
        # available, fall back to considering all visible entries.
        used_ids = set()
        for obj in ds.get("objects", {}).values():
            if obj.get("bitmap"):
                used_ids |= set(obj["bitmap"])

        for eid, (y, cr, cb, alpha) in palette.items():
            if alpha < _MIN_ALPHA:
                continue
            if used_ids and eid not in used_ids:
                continue
            if y > max_y:
                max_y = y

            if y > _MIN_Y:
                has_bright = True
                ch = _bt2020_max_channel(y, cr, cb)
                if ch > max_pq_channel:
                    max_pq_channel = ch

            # Achromatic check: Cb and Cr both near 128
            if abs(cb - 128) <= _ACHRO_TOL and abs(cr - 128) <= _ACHRO_TOL:
                if max_achromatic_y is None or y > max_achromatic_y:
                    max_achromatic_y = y

    # --- Decision logic ---
    if num_palettes == 0 or not has_bright:
        return _result(None, "low", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 1. PQ implausibility test (strongest SDR signal).
    #    If the max PQ channel value exceeds 0.75, at least one visible
    #    entry decodes to >1000 nits under PQ — almost certainly SDR.
    if max_pq_channel > _PQ_IMPLAUSIBLE:
        return _result("sdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 2. Clear Y-value signals (achromatic preferred, then global max).
    ref_y = max_achromatic_y if max_achromatic_y is not None and max_achromatic_y > _MIN_Y else None

    if ref_y is not None and ref_y >= _Y_SDR_HIGH:
        return _result("sdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)
    if max_y >= _Y_SDR_HIGH:
        return _result("sdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    if ref_y is not None and ref_y <= _Y_HDR_HIGH:
        return _result("hdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)
    if max_y <= _Y_HDR_HIGH:
        return _result("hdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 3. PQ plausibility test (strong HDR signal).
    #    If all channels stay below 0.65, the PQ interpretation gives
    #    <400 nits — perfectly reasonable for HDR subtitles.
    if max_pq_channel <= _PQ_PLAUSIBLE:
        return _result("hdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 4. Ambiguous zone (170 < max_y < 210, 0.65 < max_pq < 0.75).
    #    Fall back to medium-confidence Y threshold.
    if max_y >= _Y_SDR_MED:
        return _result("sdr", "medium", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)
    return _result("hdr", "medium", max_y, max_achromatic_y,
                   max_pq_channel, num_palettes)


def _result(verdict, confidence, max_y, max_achromatic_y,
            max_pq_channel, num_palettes):
    """Build a detection result dict."""
    return {
        "verdict": verdict,
        "confidence": confidence,
        "max_y": max_y,
        "max_achromatic_y": max_achromatic_y,
        "max_pq_channel": round(max_pq_channel, 3),
        "num_palettes": num_palettes,
    }


def format_detection(result: dict) -> str:
    """Format a detection result for terminal output.

    Returns a human-readable string like:
        "HDR (Y-max: 143, PQ-max: 0.586, 24 palettes)"
        "SDR (Y-max: 178, PQ-max: 0.924, 48 palettes)"
    """
    v = result["verdict"]
    if v is None:
        return "unknown (insufficient palette data)"

    label = v.upper()
    parts = [f"Y-max: {result['max_y']}"]
    if result.get("max_pq_channel"):
        parts.append(f"PQ-max: {result['max_pq_channel']:.3f}")
    if result["max_achromatic_y"] is not None:
        parts.append(f"achromatic Y-max: {result['max_achromatic_y']}")
    parts.append(f"{result['num_palettes']} palette(s)")
    if result["confidence"] != "high":
        parts.append(f"{result['confidence']} confidence")

    return f"{label} ({', '.join(parts)})"
