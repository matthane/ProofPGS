"""Interactive prompts for track and count selection."""

import sys


def select_tracks_interactive(tracks: list) -> list:
    """Prompt the user to select which PGS tracks to process."""
    print("Which tracks do you want to process?")
    print("  [Enter]    All tracks")
    print("  [numbers]  Specific tracks, e.g. 0,2,3")
    try:
        choice = input("> ").strip()
    except EOFError:
        print()
        return list(range(len(tracks)))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    if not choice:
        return list(range(len(tracks)))

    try:
        indices = [int(x.strip()) for x in choice.split(",")]
        valid = [i for i in indices if 0 <= i < len(tracks)]
        if not valid:
            print("  No valid track numbers entered. Processing all tracks.")
            return list(range(len(tracks)))
        return valid
    except ValueError:
        print("  Invalid input. Processing all tracks.")
        return list(range(len(tracks)))


def select_count_interactive() -> int | None:
    """Prompt the user for how many display sets to process per track."""
    print("How many display sets (subtitle images) to process per track?")
    print("  [Enter]    10 (default — fast preview)")
    print("  [number]   Custom count")
    print("  [a]        All (reads entire file)")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        print()
        return 10
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    if not choice:
        return 10
    if choice in ("a", "all"):
        return None
    try:
        n = int(choice)
        return n if n > 0 else 10
    except ValueError:
        print("  Invalid input. Using default (10).")
        return 10
