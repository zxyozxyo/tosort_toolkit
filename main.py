"""
ToSort Toolkit - RomVault Cleaning Pipeline
Main application entry point.
Requires: pip install pywebview py7zr rarfile zstandard
"""

import webview
import os
import threading
import time

from api import ToSortAPI
from dat_merger import DatMergerAPI
from ia_uploader import IAUploaderAPI
from rclone_gui import RCloneAPI
from ia_folder_packer import IAFolderPackerAPI
from scene_recreator import SceneRecreatorAPI
from misc_tools import MiscToolsAPI
from srrdb_tool import SrrdbToolAPI

_dat_api = None
_ia_api  = None


def open_tosort():
    """Open the ToSort Pipeline window."""
    _api = ToSortAPI()
    _api.open_dat_merger      = open_dat_merger
    _api._ia_uploader_fn      = open_ia_uploader
    _api.open_ia_uploader     = open_ia_uploader
    _api.open_rclone          = open_rclone
    _api.open_folder_packer   = open_folder_packer
    _api.open_scene_recreator = open_scene_recreator
    w = webview.create_window(
        title="ToSort Pipeline — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "index.html"),
        js_api=_api,
        width=1050,
        height=800,
        min_size=(800, 600),
        background_color="#0d0f12",
    )
    _api.set_window(w)


def open_dat_merger():
    """Open or re-create the DAT Tools window."""
    global _dat_api
    _dat_api = DatMergerAPI()
    dat_window = webview.create_window(
        title="DAT Tools — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "dat_merger.html"),
        js_api=_dat_api,
        width=960,
        height=780,
        min_size=(700, 500),
        background_color="#0d0f12",
    )
    _dat_api.set_window(dat_window)


def open_folder_packer():
    """Open IA Folder Packer window."""
    _fp_api = IAFolderPackerAPI()
    fp_window = webview.create_window(
        title="IA Prep Packer: Folder Packer — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "ia_folder_packer.html"),
        js_api=_fp_api,
        width=800,
        height=800,
        min_size=(600, 500),
        background_color="#0d0f12",
    )
    _fp_api.set_window(fp_window)


def open_scene_recreator():
    """Open Scene ZIP Recreator window."""
    _sr_api = SceneRecreatorAPI()
    sr_window = webview.create_window(
        title="Scene ZIP Recreator — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "scene_recreator.html"),
        js_api=_sr_api,
        width=900,
        height=900,
        min_size=(700, 600),
        background_color="#0d0f12",
    )
    _sr_api.set_window(sr_window)


def open_rclone():
    """Open RClone IA Uploader window."""
    _rc_api = RCloneAPI()
    _rc_api.open_folder_packer = open_folder_packer
    rc_window = webview.create_window(
        title="IA RClone Uploader — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "rclone_gui.html"),
        js_api=_rc_api,
        width=900,
        height=950,
        min_size=(700, 600),
        background_color="#0d0f12",
    )
    _rc_api.set_window(rc_window)


def open_ia_uploader():
    """Open or re-create the IA Uploader window."""
    global _ia_api
    _ia_api = IAUploaderAPI()
    _ia_api.open_folder_packer = open_folder_packer
    ia_window = webview.create_window(
        title="Internet Archive Uploader — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "ia_uploader.html"),
        js_api=_ia_api,
        width=900,
        height=900,
        min_size=(700, 600),
        background_color="#0d0f12",
    )
    _ia_api.set_window(ia_window)


def open_srrdb_tool():
    """Open srrdb Scene Rebuilder window."""
    _srr_api = SrrdbToolAPI()
    srr_window = webview.create_window(
        title="srrdb Scene Rebuilder — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "srrdb_tool.html"),
        js_api=_srr_api,
        width=980,
        height=780,
        min_size=(700, 550),
        background_color="#0d0f12",
    )
    _srr_api.set_window(srr_window)


def open_misc_tools():
    """Open Miscellaneous Tools window."""
    _misc_api = MiscToolsAPI()
    misc_window = webview.create_window(
        title="Miscellaneous Tools — ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "misc_tools.html"),
        js_api=_misc_api,
        width=820,
        height=820,
        min_size=(600, 500),
        background_color="#0d0f12",
    )
    _misc_api.set_window(misc_window)


class LauncherAPI:
    """Minimal API exposed to the home/launcher page."""
    def __init__(self):
        self._window = None
        # Config-only Misc Tools instance (auto_loop=False → no periodic timer;
        # the launcher runs the one-shot startup backup instead). Backup config
        # lives in misc_tools.json, shared with the Misc Tools window.
        self._misc = MiscToolsAPI(auto_loop=False)

    def set_window(self, w):
        self._window = w

    def open_tosort(self):        open_tosort()
    def open_dat_merger(self):    open_dat_merger()
    def open_ia_uploader(self):   open_ia_uploader()
    def open_folder_packer(self): open_folder_packer()
    def open_rclone(self):        open_rclone()
    def open_scene_recreator(self): open_scene_recreator()
    def open_misc_tools(self):    open_misc_tools()
    def open_srrdb_tool(self):    open_srrdb_tool()

    # --- startup auto-backup (settings shared with Misc Tools → Local Backup) ---
    def backup_startup_info(self):
        return self._misc.backup_startup_info()

    def backup_set_startup(self, enabled):
        return self._misc.backup_set_startup(enabled)

    def _home_status(self, msg, cls="info"):
        """Push a one-line backup status to the launcher page."""
        if not self._window:
            return
        try:
            import json as _j
            self._window.evaluate_js(
                "window.homeBackupStatus && window.homeBackupStatus("
                + _j.dumps(str(msg)) + ")")
        except Exception:
            pass

    def _run_startup_backup(self):
        """Runs once after the GUI is up (see webview.start). Backs up only if
        due — see MiscToolsAPI.maybe_run_startup_backup."""
        time.sleep(1.5)   # let the page finish loading before pushing status
        try:
            self._misc.maybe_run_startup_backup(log_cb=self._home_status)
        except Exception:
            pass
        try:
            if self._window:
                self._window.evaluate_js(
                    "window.refreshAutoBackup && window.refreshAutoBackup()")
        except Exception:
            pass


def main():
    api = LauncherAPI()
    window = webview.create_window(
        title="ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "home.html"),
        js_api=api,
        width=920,
        height=620,
        min_size=(700, 480),
        background_color="#0d0f12",
    )
    api.set_window(window)
    # Run the startup auto-backup check in the background once the GUI loop is up.
    webview.start(api._run_startup_backup, debug=False)


if __name__ == "__main__":
    main()
