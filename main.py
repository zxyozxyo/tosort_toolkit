"""
ToSort Toolkit - RomVault Cleaning Pipeline
Main application entry point.
Requires: pip install pywebview py7zr rarfile zstandard
"""

import webview
import os

from api import ToSortAPI
from dat_merger import DatMergerAPI

_dat_api = None


def open_dat_merger():
    """Open or re-create the DAT Tools window."""
    global _dat_api

    # Always create a fresh window - pywebview windows can't be
    # re-shown after being closed by the user
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
