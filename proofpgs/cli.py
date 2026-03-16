"""Command-line interface for ProofPGS."""

import argparse
import os
import sys

from .constants import SUP_EXTENSIONS, CONTAINER_EXTENSIONS
from .pipeline import process_sup_file, process_container
from .style import dim, error, info, success, warn


def main():
    # Windows consoles default to a legacy codepage (e.g. cp1252) that cannot
    # encode CJK characters.  Reconfigure to UTF-8 so track titles with
    # non-Latin text print correctly.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    try:
        _main()
    except KeyboardInterrupt:
        print(f"\n{dim('Interrupted.')}")
        sys.exit(130)


def _main():
    parser = argparse.ArgumentParser(
        description="PGS subtitle decoder — accepts .sup files or video "
                    "containers (MKV, M2TS, etc.).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_file", nargs="?", default=None,
                        help="Path to a .sup file or video container "
                             "(MKV, M2TS, TS, MP4, etc.)")
    parser.add_argument("--install", action="store_true",
                        help="Register Windows Explorer context menu entries "
                             "for all supported file types")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove Windows Explorer context menu entries")
    parser.add_argument("--mode", choices=["auto", "compare", "hdr", "sdr", "validate"],
                        default="auto",
                        help="Output mode. auto=detect color space (default), "
                             "compare=SDR & HDR side-by-side, "
                             "hdr=BT.2020+PQ (UHD BD), sdr=BT.709 (BD), "
                             "validate=show track info & detection only (no output)")
    parser.add_argument("--tonemap", choices=["clip", "reinhard"], default="clip",
                        help="HDR->SDR tonemapping. clip=hard clip at 203 nits ref white "
                             "(best for subtitles), reinhard=soft roll-off. Default: clip")
    parser.add_argument("--out", default=None,
                        help="Output directory. Default: pgs_output/ next to the input file")
    parser.add_argument("--first", type=int, default=None,
                        help="Decode only the first N subtitles. "
                             "For containers, interactive prompt defaults to 10")
    parser.add_argument("--tracks", default=None,
                        help="Track indices to process (comma-separated, e.g. 0,2,3). "
                             "Use 'all' for all tracks. Default: prompt interactively")
    parser.add_argument("--nocrop", action="store_true",
                        help="Output full video-frame sized PNGs instead of cropping to content")
    parser.add_argument("--threads", type=int, default=None,
                        help="Number of parallel rendering threads "
                             "(default: auto, up to 8)")
    args = parser.parse_args()

    if args.install:
        from .shellmenu import install
        install()
        return

    if args.uninstall:
        from .shellmenu import uninstall
        uninstall()
        return

    if args.input_file is None:
        parser.print_help()
        sys.exit(1)

    if not os.path.isfile(args.input_file):
        print(f"{error('[error]')} File not found: {args.input_file}", file=sys.stderr)
        sys.exit(1)

    if args.out is None:
        args.out = os.path.join(os.path.dirname(os.path.abspath(args.input_file)),
                                "pgs_output")

    ext = os.path.splitext(args.input_file)[1].lower()

    if ext in SUP_EXTENSIONS:
        print(f"{info('Reading:')} {args.input_file}")
        saved = process_sup_file(args.input_file, args.out, args.mode,
                                 args.tonemap, args.first, args.nocrop,
                                 threads=args.threads)
        if args.mode != "validate":
            print(f"\n{success('Done.')} {saved} images written to {args.out}/")
    elif ext in CONTAINER_EXTENSIONS:
        process_container(args.input_file, args.out, args.mode,
                          args.tonemap, args.first, args.nocrop,
                          tracks_arg=args.tracks, threads=args.threads)
    else:
        print(f"{warn('[warn]')} Unrecognised extension '{ext}'. "
              f"Attempting as container file...")
        process_container(args.input_file, args.out, args.mode,
                          args.tonemap, args.first, args.nocrop,
                          tracks_arg=args.tracks, threads=args.threads)
