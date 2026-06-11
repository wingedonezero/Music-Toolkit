# music_toolkit/core/scanner.py
#
# Shared path scanner for the batch tools. Dropped files/folders expand into
# {folder: [files]} groups so every folder (album) is treated as its own batch
# — multi-disc rips with CD1/CD2 subfolders become separate groups.

import os
import re


def natural_key(text: str):
    """Sort key that orders embedded numbers numerically ('2' before '10')."""
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r'(\d+)', text)]


def scan_paths(paths, extensions, recursive=True) -> dict:
    """Expands files/folders into an ordered {folder: [files]} mapping.

    `extensions` is a set like {'.flac'} (case-insensitive). Files are grouped
    by their parent folder and naturally sorted; folders are sorted too.
    """
    extensions = {ext.lower() for ext in extensions}
    groups = {}

    def add_file(path):
        if os.path.splitext(path)[1].lower() not in extensions:
            return
        folder = os.path.dirname(path)
        groups.setdefault(folder, set()).add(path)

    for path in paths:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            add_file(path)
        elif os.path.isdir(path):
            if recursive:
                for root, dirs, files in os.walk(path):
                    dirs.sort(key=natural_key)
                    for name in files:
                        add_file(os.path.join(root, name))
            else:
                for name in sorted(os.listdir(path), key=natural_key):
                    full = os.path.join(path, name)
                    if os.path.isfile(full):
                        add_file(full)

    return {
        folder: sorted(groups[folder], key=lambda p: natural_key(os.path.basename(p)))
        for folder in sorted(groups, key=natural_key)
    }
