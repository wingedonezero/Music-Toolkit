# music_toolkit/gui/batch_tree.py
#
# Folder-grouped file tree shared by the toolkit's batch tools. Top-level rows
# are folders and their children are the files inside, so every album/folder
# reads as its own batch and per-folder results stay visually separated.

import os

from PySide6 import QtWidgets, QtCore, QtGui


class BatchFileTree(QtWidgets.QTreeWidget):
    """Accepts file/folder drops and shows files grouped under their folder.

    The widget only manages presentation; owning tools scan dropped paths in a
    worker thread and feed rows back in via add_file()/set_file_texts().
    """

    paths_dropped = QtCore.Signal(list)
    files_removed = QtCore.Signal(list)

    PATH_ROLE = QtCore.Qt.ItemDataRole.UserRole

    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(columns))
        self.setHeaderLabels(columns)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self._folder_items = {}
        self._file_items = {}
        self._removal_allowed = True

        folder_font = self.font()
        folder_font.setBold(True)
        self._folder_font = folder_font

    # ----- queries -----

    def folder_count(self) -> int:
        return len(self._folder_items)

    def file_count(self) -> int:
        return len(self._file_items)

    def has_file(self, path: str) -> bool:
        return path in self._file_items

    def all_files(self) -> list:
        return list(self._file_items.keys())

    def all_folders(self) -> list:
        return list(self._folder_items.keys())

    def files_in_folder(self, folder: str) -> list:
        item = self._folder_items.get(folder)
        if not item:
            return []
        return [item.child(i).data(0, self.PATH_ROLE) for i in range(item.childCount())]

    def file_item(self, path: str):
        return self._file_items.get(path)

    def folder_item(self, folder: str):
        return self._folder_items.get(folder)

    def folder_of_item(self, item):
        """Returns the folder path an item belongs to (itself if a folder row)."""
        if item is None:
            return None
        if item.parent() is None:
            return item.data(0, self.PATH_ROLE)
        return os.path.dirname(item.data(0, self.PATH_ROLE))

    def set_removal_allowed(self, allowed: bool):
        """Blocks Remove/Clear from the context menu (e.g. while a batch runs)."""
        self._removal_allowed = allowed

    # ----- population -----

    def add_folder(self, folder: str):
        item = self._folder_items.get(folder)
        if item is None:
            item = QtWidgets.QTreeWidgetItem(self, [folder])
            item.setData(0, self.PATH_ROLE, folder)
            item.setToolTip(0, folder)
            for col in range(self.columnCount()):
                item.setFont(col, self._folder_font)
            item.setExpanded(True)
            self._folder_items[folder] = item
        return item

    def add_file(self, folder: str, path: str, texts: list):
        """Adds a file row under its folder; returns None if already present."""
        if path in self._file_items:
            return None
        parent = self.add_folder(folder)
        item = QtWidgets.QTreeWidgetItem(parent, texts)
        item.setData(0, self.PATH_ROLE, path)
        item.setToolTip(0, path)
        self._file_items[path] = item
        return item

    # ----- updates -----

    def set_file_texts(self, path: str, texts_by_column: dict):
        item = self._file_items.get(path)
        if not item:
            return
        for col, text in texts_by_column.items():
            item.setText(col, text)
            item.setToolTip(col, text)

    def set_file_color(self, path: str, color):
        item = self._file_items.get(path)
        if not item:
            return
        brush = QtGui.QBrush(color) if color is not None else QtGui.QBrush()
        for col in range(self.columnCount()):
            item.setForeground(col, brush)

    def set_folder_text(self, folder: str, column: int, text: str):
        item = self._folder_items.get(folder)
        if item:
            item.setText(column, text)

    def clear_all(self) -> list:
        removed = list(self._file_items.keys())
        self.clear()
        self._folder_items.clear()
        self._file_items.clear()
        return removed

    # ----- removal / context menu -----

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self)
        remove_action = menu.addAction("Remove Selected")
        clear_action = menu.addAction("Clear All")
        menu.addSeparator()
        expand_action = menu.addAction("Expand All")
        collapse_action = menu.addAction("Collapse All")
        remove_action.setEnabled(self._removal_allowed and bool(self.selectedItems()))
        clear_action.setEnabled(self._removal_allowed and bool(self._file_items))

        chosen = menu.exec(event.globalPos())
        if chosen is remove_action:
            self.remove_selected()
        elif chosen is clear_action:
            removed = self.clear_all()
            if removed:
                self.files_removed.emit(removed)
        elif chosen is expand_action:
            self.expandAll()
        elif chosen is collapse_action:
            self.collapseAll()

    def remove_selected(self) -> list:
        # Selected folder rows take all their children with them.
        file_items = set()
        for item in self.selectedItems():
            if item.parent() is None:
                for i in range(item.childCount()):
                    file_items.add(item.child(i))
            else:
                file_items.add(item)

        removed = []
        for item in file_items:
            path = item.data(0, self.PATH_ROLE)
            item.parent().removeChild(item)
            self._file_items.pop(path, None)
            removed.append(path)

        # Drop folder rows that lost all their files.
        for folder, folder_item in list(self._folder_items.items()):
            if folder_item.childCount() == 0:
                self.takeTopLevelItem(self.indexOfTopLevelItem(folder_item))
                del self._folder_items[folder]

        if removed:
            self.files_removed.emit(removed)
        return removed

    # ----- drag & drop -----

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.paths_dropped.emit(paths)
