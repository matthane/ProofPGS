"""Windows Explorer context menu integration for ProofPGS."""

import sys
from pathlib import Path

from .constants import SUP_EXTENSIONS, CONTAINER_EXTENSIONS
from .style import error, success, dim

_MENU_NAME = "ProofPGS"
_SUBMENU_SUP = "ProofPGS.SupMenu"
_SUBMENU_CONTAINER = "ProofPGS.ContainerMenu"

# (registry_name, display_label, mode_value, use_pause)
# use_pause: validate exits immediately so needs & pause to keep the window open
_COMMON_MODES = [
    ("01_auto",     "Auto export (detect color space)",          "auto",     False),
    ("02_compare",  "Compare (SDR && HDR side-by-side)",     "compare",  False),
    ("03_hdr",      "Export as HDR (BT.2020+PQ)",                   "hdr",      False),
    ("04_sdr",      "Export as SDR (BT.709)",                        "sdr",      False),
]

# .sup files are parsed directly — no FFmpeg analysis budget, single track
_SUP_MODES = _COMMON_MODES + [
    ("05_validate", "Validate", "validate", True),
]

# Containers need FFmpeg analysis; validate-fast skips sparse tracks
_CONTAINER_MODES = _COMMON_MODES + [
    ("05_validate",      "Validate (show track info only, may be slow)", "validate", True),
    ("06_validate_fast", "Validate fast (skips sparse tracks)",          "validate-fast", False),
]


def _all_extensions():
    """Return sorted list of all supported file extensions."""
    return sorted(SUP_EXTENSIONS | CONTAINER_EXTENSIONS)


def _is_pip_installed() -> bool:
    """Check whether ProofPGS was installed as a pip package."""
    from importlib.metadata import PackageNotFoundError, metadata
    try:
        metadata("proofpgs")
        return True
    except PackageNotFoundError:
        return False


def _project_root() -> str:
    """Return the project root directory (parent of the proofpgs package)."""
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _icon_path() -> str:
    """Return the icon path matching the current Windows theme."""
    import winreg
    variant = "light"  # light icon for dark backgrounds (default)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            if val == 1:
                variant = "dark"  # dark icon for light backgrounds
    except OSError:
        pass
    return str(Path(__file__).resolve().parent / "assets" / f"proofpgs-icon-{variant}.ico")


def _build_command(mode: str, use_pause: bool, *,
                   proofpgs_exe: str | None = None,
                   python_exe: str | None = None,
                   project_dir: str | None = None) -> str:
    """Build the shell command string for a context menu entry.

    When *proofpgs_exe* is given (pip install), the command invokes it directly.
    Otherwise *python_exe* and *project_dir* are used (source / archive install).
    """
    if proofpgs_exe:
        base = f'"{proofpgs_exe}" "%1" --mode {mode}'
    else:
        base = f'cd /d "{project_dir}" && "{python_exe}" -m proofpgs "%1" --mode {mode}'
    if use_pause:
        return f'cmd.exe /c "{base} & pause"'
    return f'cmd.exe /k "{base}"'


def _notify_shell():
    """Tell Explorer to refresh file associations."""
    import ctypes
    SHCNE_ASSOCCHANGED = 0x08000000
    SHCNF_IDLIST = 0x0000
    ctypes.windll.shell32.SHChangeNotify(
        SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
    )


def _delete_key_tree(root, subkey: str):
    """Recursively delete a registry key and all its children."""
    import winreg
    try:
        hkey = winreg.OpenKey(root, subkey, 0,
                              winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return False

    # Enumerate and delete children first (depth-first)
    while True:
        try:
            child = winreg.EnumKey(hkey, 0)
            _delete_key_tree(root, f"{subkey}\\{child}")
        except OSError:
            break
    winreg.CloseKey(hkey)
    winreg.DeleteKey(root, subkey)
    return True


def _clean_empty_parents(root, subkey: str):
    """Walk up the path and remove empty parent keys."""
    import winreg
    parts = subkey.split("\\")
    while parts:
        path = "\\".join(parts)
        try:
            hkey = winreg.OpenKey(root, path, 0, winreg.KEY_READ)
            # Check if it has any subkeys or values
            info = winreg.QueryInfoKey(hkey)
            winreg.CloseKey(hkey)
            if info[0] == 0 and info[1] == 0:
                winreg.DeleteKey(root, path)
            else:
                break
        except OSError:
            break
        parts.pop()


def install():
    """Register Windows Explorer context menu entries for ProofPGS."""
    if sys.platform != "win32":
        print(f"{error('[error]')} Context menu integration is only available on Windows.",
              file=sys.stderr)
        sys.exit(1)

    import winreg
    import shutil

    pip_installed = _is_pip_installed()

    if pip_installed:
        proofpgs_exe = shutil.which("proofpgs")
        if not proofpgs_exe:
            print(f"{error('[error]')} proofpgs is pip-installed but the "
                  f"'proofpgs' command was not found on PATH.", file=sys.stderr)
            sys.exit(1)
        cmd_kwargs = {"proofpgs_exe": proofpgs_exe}
    else:
        python_exe = sys.executable
        if not python_exe:
            print(f"{error('[error]')} Could not determine Python executable path.",
                  file=sys.stderr)
            sys.exit(1)
        cmd_kwargs = {"python_exe": python_exe, "project_dir": _project_root()}

    sup_exts = sorted(SUP_EXTENSIONS)
    container_exts = sorted(CONTAINER_EXTENSIONS)

    # --- Create submenu commands for each extension group ---
    for submenu_key, modes in [(_SUBMENU_SUP, _SUP_MODES),
                                (_SUBMENU_CONTAINER, _CONTAINER_MODES)]:
        submenu_shell = f"Software\\Classes\\{submenu_key}\\shell"
        for reg_name, label, mode, use_pause in modes:
            verb_path = f"{submenu_shell}\\{reg_name}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, verb_path) as key:
                winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, label)

            cmd_path = f"{verb_path}\\command"
            command = _build_command(mode, use_pause, **cmd_kwargs)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_path) as key:
                winreg.SetValue(key, "", winreg.REG_SZ, command)

    # --- Register per-extension shell verb ---
    icon = _icon_path()
    for ext, submenu_key in ([(e, _SUBMENU_SUP) for e in sup_exts] +
                              [(e, _SUBMENU_CONTAINER) for e in container_exts]):
        verb_path = (f"Software\\Classes\\SystemFileAssociations"
                     f"\\{ext}\\shell\\{_MENU_NAME}")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, verb_path) as key:
            winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, _MENU_NAME)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(key, "ExtendedSubCommandsKey", 0,
                              winreg.REG_SZ, submenu_key)

    _notify_shell()

    all_exts = " ".join(sorted(sup_exts + container_exts))
    print(f"{success('Registered')} context menu for "
          f"{len(sup_exts) + len(container_exts)} file types:")
    print(f"  {all_exts}")
    print()
    print(f"Subtitle files ({' '.join(sup_exts)}):")
    for _, label, _, _ in _SUP_MODES:
        print(f"  - {label}")
    print(f"Container files ({' '.join(container_exts)}):")
    for _, label, _, _ in _CONTAINER_MODES:
        print(f"  - {label}")
    print()
    print(dim("Note: On Windows 11, right-click and choose 'Show more options' "
              "to see the submenu."))
    if pip_installed:
        print(dim(f"Command: {proofpgs_exe}"))
    else:
        print(dim(f"Python: {cmd_kwargs['python_exe']}"))
        print(dim(f"Project: {cmd_kwargs['project_dir']}"))


def uninstall():
    """Remove all Windows Explorer context menu entries for ProofPGS."""
    if sys.platform != "win32":
        print(f"{error('[error]')} Context menu integration is only available on Windows.",
              file=sys.stderr)
        sys.exit(1)

    import winreg

    removed = 0

    # --- Remove shared submenus ---
    for submenu_key in (_SUBMENU_SUP, _SUBMENU_CONTAINER):
        submenu_path = f"Software\\Classes\\{submenu_key}"
        if _delete_key_tree(winreg.HKEY_CURRENT_USER, submenu_path):
            removed += 1

    # --- Clean up legacy single submenu from older installs ---
    legacy_path = "Software\\Classes\\ProofPGS.SubMenu"
    if _delete_key_tree(winreg.HKEY_CURRENT_USER, legacy_path):
        removed += 1

    # --- Remove per-extension verbs ---
    for ext in _all_extensions():
        verb_path = (f"Software\\Classes\\SystemFileAssociations"
                     f"\\{ext}\\shell\\{_MENU_NAME}")
        if _delete_key_tree(winreg.HKEY_CURRENT_USER, verb_path):
            removed += 1
            # Clean up empty parent keys left behind
            shell_path = (f"Software\\Classes\\SystemFileAssociations"
                          f"\\{ext}\\shell")
            _clean_empty_parents(winreg.HKEY_CURRENT_USER, shell_path)

    _notify_shell()

    if removed:
        print(f"{success('Removed')} Windows Explorer context menu entries for ProofPGS.")
    else:
        print("No ProofPGS context menu entries found.")
