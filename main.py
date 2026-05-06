"""
ToSort Toolkit - RomVault Cleaning Pipeline
Main application entry point.
Requires: pip install pywebview py7zr rarfile zstandard
"""

import webview
import sys
import os
import threading

from api import ToSortAPI
from dat_merger import DatMergerAPI

dat_window = None
dat_api = None


def open_dat_merger():
    """Called from the main window to open/focus the DAT merger window."""
    global dat_window, dat_api
    if dat_window is not None:
        try:
            dat_window.show()
            dat_window.on_top = True
            dat_window.on_top = False
            return
        except Exception:
            pass

    dat_api = DatMergerAPI()
    dat_window = webview.create_window(
        title="DAT Merger \u2014 ToSort Toolkit",
        url=os.path.join(os.path.dirname(__file__), "gui", "dat_merger.html"),
        js_api=dat_api,
        width=900,
        height=750,
        min_size=(700, 500),
        background_color="#0d0f12",
    )
    dat_api.set_window(dat_window)


def main():
    api = ToSortAPI()
    api.open_dat_merger = open_dat_merger

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
