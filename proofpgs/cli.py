"""Command-line interface for ProofPGS."""

import argparse
import os
import sys

from .constants import SUP_EXTENSIONS, CONTAINER_EXTENSIONS
from .pipeline import process_sup_file, process_container


def main():
    try:
        _main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


def _main():
    parser = argparse.ArgumentParser(
        description="PGS subtitle decoder — accepts .sup files or video "
                    "containers (MKV, M2TS, etc.).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_file",
                        help="Path to a .sup file or video container "
                             "(MKV, M2TS, TS, MP4, etc.)")
    parser.add_argument("--mode", choices=["auto", "compare", "hdr", "sdr"], default="auto",
                        help="Output mode. auto=detect color space (default), "
                             "compare=SDR & HDR side-by-side, "
                             "hdr=BT.2020+PQ (UHD BD), sdr=BT.709 (BD)")
    parser.add_argument("--tonemap", choices=["clip", "reinhard"], default="clip",
                        help="HDR->SDR tonemapping. clip=hard clip at 203 nits ref white "
                             "(best for subtitles), reinhard=soft roll-off. Default: clip")
    parser.add_argument("--out", default=None,
                        help="Output directory. Default: pgs_output/ next to the input file")
    parser.add_argument("--first", type=int, default=None,
                        help="Decode only the first N display sets. "
                             "For containers, interactive prompt defaults to 10")
    parser.add_argument("--tracks", default=None,
                        help="Track indices to process (comma-separated, e.g. 0,2,3). "
                             "Use 'all' for all tracks. Default: prompt interactively")
    parser.add_argument("--nocrop", action="store_true",
                        help="Output full video-frame sized PNGs instead of cropping to content")
    args = parser.parse_args()

    if not os.path.isfile(args.input_file):
        print(f"[error] File not found: {args.input_file}", file=sys.stderr)
        sys.exit(1)

    if args.out is None:
        args.out = os.path.join(os.path.dirname(os.path.abspath(args.input_file)),
                                "pgs_output")

    ext = os.path.splitext(args.input_file)[1].lower()

    if ext in SUP_EXTENSIONS:
        print(f"Reading: {args.input_file}")
        saved = process_sup_file(args.input_file, args.out, args.mode,
                                 args.tonemap, args.first, args.nocrop)
        print(f"\nDone. {saved} images written to {args.out}/")
    elif ext in CONTAINER_EXTENSIONS:
        process_container(args.input_file, args.out, args.mode,
                          args.tonemap, args.first, args.nocrop,
                          tracks_arg=args.tracks)
    else:
        print(f"[warn] Unrecognised extension '{ext}'. "
              f"Attempting as container file...")
        process_container(args.input_file, args.out, args.mode,
                          args.tonemap, args.first, args.nocrop,
                          tracks_arg=args.tracks)
