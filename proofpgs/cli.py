"""Command-line interface for ProofPGS."""

import argparse
import os
import sys

from .constants import SUP_EXTENSIONS, CONTAINER_EXTENSIONS, parse_timestamp
from .libpgs import check_libpgs
from .pipeline import process_sup_file, process_container
from .style import dim, info, status_err, status_ok, warn


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
        os._exit(130)


def _main():
    parser = argparse.ArgumentParser(
        description="PGS subtitle decoder — accepts .sup files or video "
                    "containers (MKV, M2TS, etc.).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_file", nargs="?", default=None,
                        help="Path to a .sup file or video container "
                             "(MKV, M2TS)")
    parser.add_argument("--install", action="store_true",
                        help="Register file manager context menu entries "
                             "for all supported file types")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove file manager context menu entries")
    parser.add_argument("--mode", choices=["auto", "compare", "hdr", "sdr",
                                          "validate", "validate-fast"],
                        default="auto",
                        help="Output mode. auto=detect color space (default), "
                             "compare=SDR & HDR side-by-side, "
                             "hdr=BT.2020+PQ (UHD BD), sdr=BT.709 (BD), "
                             "validate=show track info & detection only (no output), "
                             "validate-fast=budgeted validation with option to "
                             "fully validate remaining tracks")
    parser.add_argument("--tonemap", choices=["clip", "reinhard"], default="clip",
                        help="HDR->SDR tonemapping. clip=hard clip at 203 nits ref white "
                             "(best for subtitles), reinhard=soft roll-off. Default: clip")
    parser.add_argument("--out", default=None,
                        help="Output directory. Default: <filename>_pgs_output/ next to the input file")
    parser.add_argument("--first", type=int, default=None,
                        help="Decode only the first N subtitles. "
                             "For containers, interactive prompt defaults to 10")
    parser.add_argument("--tracks", default=None,
                        help="Track indices to process (1-based, comma-separated, e.g. 1,3,4). "
                             "Use 'all' for all tracks. Default: prompt interactively")
    parser.add_argument("--nocrop", action="store_true",
                        help="Output full video-frame sized PNGs instead of cropping to content")
    parser.add_argument("--start", default=None,
                        help="Start timestamp for extraction "
                             "(e.g. 0:05:00, 5:00, 300)")
    parser.add_argument("--end", default=None,
                        help="End timestamp for extraction "
                             "(e.g. 0:10:00, 10:00, 600)")
    parser.add_argument("--threads", type=int, default=None,
                        help="Number of parallel rendering threads "
                             "(default: auto, up to 8)")
    args = parser.parse_args()

    # Validate timestamps early.
    for flag in ("start", "end"):
        val = getattr(args, flag)
        if val is not None:
            try:
                parse_timestamp(val)
            except ValueError as e:
                print(status_err(f"--{flag}: {e}"), file=sys.stderr)
                sys.exit(1)
    if args.start is not None and args.end is not None:
        if parse_timestamp(args.end) <= parse_timestamp(args.start):
            print(status_err("--end must be after --start"), file=sys.stderr)
            sys.exit(1)

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
        print(status_err(f"File not found: {args.input_file}"), file=sys.stderr)
        sys.exit(1)

    if args.out is None:
        stem = os.path.splitext(os.path.basename(os.path.abspath(args.input_file)))[0]
        args.out = os.path.join(os.path.dirname(os.path.abspath(args.input_file)),
                                f"{stem}_pgs_output")

    libpgs_path = check_libpgs()
    ext = os.path.splitext(args.input_file)[1].lower()

    if ext in SUP_EXTENSIONS:
        print(f"{info('Reading:')} {args.input_file}")
        saved = process_sup_file(args.input_file, args.out, args.mode,
                                 args.tonemap, args.first, args.nocrop,
                                 libpgs_path=libpgs_path,
                                 threads=args.threads,
                                 interactive=True,
                                 start=args.start, end=args.end)
        if args.mode not in ("validate", "validate-fast"):
            print()
            print(status_ok(f"{saved} images written to {args.out}/"))
    elif ext in CONTAINER_EXTENSIONS:
        process_container(args.input_file, args.out, args.mode,
                          args.tonemap, args.first, args.nocrop,
                          libpgs_path=libpgs_path,
                          tracks_arg=args.tracks, threads=args.threads,
                          start=args.start, end=args.end)
    else:
        print(warn(f"Unrecognised extension '{ext}'. "
                   f"Attempting as container file..."))
        process_container(args.input_file, args.out, args.mode,
                          args.tonemap, args.first, args.nocrop,
                          libpgs_path=libpgs_path,
                          tracks_arg=args.tracks, threads=args.threads,
                          start=args.start, end=args.end)
