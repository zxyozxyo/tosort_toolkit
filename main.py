"""
ToSort Toolkit - RomVault Cleaning Pipeline
Main application entry point.
Requires: pip install pywebview py7zr rarfile zstandard
"""

import webview
import os

from api import ToSortAPI
from dat_merger import DatMergerAPI
from ia_uploader import IAUploaderAPI
from rclone_gui import RCloneAPI
from ia_folder_packer import IAFolderPackerAPI

_dat_api = None
_ia_api  = None


def open_dat_merger():
    """Open or re-create the DAT Tools window."""
    global _dat_api
    _dat_api = DatMergerAPI()
    dat_window = webview.create_window(
        title="DAT Tools \u2014 ToSort Toolkit",
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
        title="IA Folder Packer \u2014 ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "ia_folder_packer.html"),
        js_api=_fp_api,
        width=800,
        height=800,
        min_size=(600, 500),
        background_color="#0d0f12",
    )
    _fp_api.set_window(fp_window)


def open_rclone():
    """Open RClone IA Uploader window."""
    _rc_api = RCloneAPI()
    rc_window = webview.create_window(
        title="RClone IA Uploader \u2014 ToSort Toolkit",
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
    ia_window = webview.create_window(
        title="Internet Archive Uploader \u2014 ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "ia_uploader.html"),
        js_api=_ia_api,
        width=900,
        height=900,
        min_size=(700, 600),
        background_color="#0d0f12",
    )
    _ia_api.set_window(ia_window)


def main():
    api = ToSortAPI()
    api.open_dat_merger  = open_dat_merger
    # Wire IA uploader — sets the function so JS api call works
    api._ia_uploader_fn  = open_ia_uploader
    api.open_ia_uploader = open_ia_uploader
    api.open_rclone        = open_rclone
    api.open_folder_packer = open_folder_packer

    window = webview.create_window(
        title="ToSort Toolkit \u2014 RomVault Cleaning Pipeline",
        url=os.path.join(os.path.dirname(__file__), "gui", "index.html"),
        js_api=api,
        width=1050,
        height=800,
        min_size=(800, 600),
        background_color="#0d0f12",
    )

    api.set_window(window)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
