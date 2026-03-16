"""Windows Explorer context menu integration for ProofPGS."""

import sys
from pathlib import Path

from .constants import SUP_EXTENSIONS, CONTAINER_EXTENSIONS
from .style import error, success, dim

_MENU_NAME = "ProofPGS"
_SUBMENU_KEY = "ProofPGS.SubMenu"

# (registry_name, display_label, mode_value, use_pause)
# use_pause: validate exits immediately so needs & pause to keep the window open
_MODES = [
    ("01_auto",     "Auto export (detect color space)",      "auto",     False),
    ("02_compare",  "Compare (SDR && HDR side-by-side)", "compare",  False),
    ("03_hdr",      "Export as HDR (BT.2020+PQ)",               "hdr",      False),
    ("04_sdr",      "Export as SDR (BT.709)",                    "sdr",      False),
    ("05_validate", "Validate (show track info only)", "validate", True),
]


def _all_extensions():
    """Return sorted list of all supported file extensions."""
    return sorted(SUP_EXTENSIONS | CONTAINER_EXTENSIONS)


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


def _build_command(python_exe: str, project_dir: str,
                   mode: str, use_pause: bool) -> str:
    """Build the shell command string for a context menu entry."""
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

    python_exe = sys.executable
    if not python_exe:
        print(f"{error('[error]')} Could not determine Python executable path.",
              file=sys.stderr)
        sys.exit(1)

    project_dir = _project_root()
    extensions = _all_extensions()

    # --- Create shared submenu commands ---
    submenu_shell = f"Software\\Classes\\{_SUBMENU_KEY}\\shell"

    for reg_name, label, mode, use_pause in _MODES:
        # Create the verb key
        verb_path = f"{submenu_shell}\\{reg_name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, verb_path) as key:
            winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, label)

        # Create the command subkey
        cmd_path = f"{verb_path}\\command"
        command = _build_command(python_exe, project_dir, mode, use_pause)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_path) as key:
            winreg.SetValue(key, "", winreg.REG_SZ, command)

    # --- Register per-extension shell verb ---
    icon = _icon_path()
    for ext in extensions:
        verb_path = (f"Software\\Classes\\SystemFileAssociations"
                     f"\\{ext}\\shell\\{_MENU_NAME}")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, verb_path) as key:
            winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, _MENU_NAME)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(key, "ExtendedSubCommandsKey", 0,
                              winreg.REG_SZ, _SUBMENU_KEY)

    _notify_shell()

    ext_list = " ".join(extensions)
    print(f"{success('Registered')} context menu for {len(extensions)} file types:")
    print(f"  {ext_list}")
    print()
    print("Right-click any of these file types to see the ProofPGS submenu:")
    for _, label, _, _ in _MODES:
        print(f"  - {label}")
    print()
    print(dim("Note: On Windows 11, right-click and choose 'Show more options' "
              "to see the submenu."))
    print(dim(f"Python: {python_exe}"))
    print(dim(f"Project: {project_dir}"))


def uninstall():
    """Remove all Windows Explorer context menu entries for ProofPGS."""
    if sys.platform != "win32":
        print(f"{error('[error]')} Context menu integration is only available on Windows.",
              file=sys.stderr)
        sys.exit(1)

    import winreg

    removed = 0

    # --- Remove shared submenu ---
    submenu_path = f"Software\\Classes\\{_SUBMENU_KEY}"
    if _delete_key_tree(winreg.HKEY_CURRENT_USER, submenu_path):
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
