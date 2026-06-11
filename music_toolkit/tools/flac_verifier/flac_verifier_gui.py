# music_toolkit/tools/flac_verifier/flac_verifier_gui.py

import os
import shutil

from PySide6 import QtWidgets, QtCore, QtGui

from music_toolkit.gui.batch_tree import BatchFileTree
from . import flac_verifier_config as config
from . import flac_verifier_core as core

COLUMNS = ['File', 'Size', 'Specs', 'Duration', 'MD5 (STREAMINFO)', 'Status', 'Details']
(COL_FILE, COL_SIZE, COL_SPECS, COL_DURATION, COL_MD5, COL_STATUS, COL_DETAILS) = range(len(COLUMNS))

STATUS_COLORS = {
    core.STATUS_OK: QtGui.QColor('#3fa34d'),
    core.STATUS_OK_NO_MD5: QtGui.QColor('#d98e04'),
    core.STATUS_FAILED: QtGui.QColor('#e05252'),
    core.STATUS_ERROR: QtGui.QColor('#e05252'),
    core.STATUS_CHECKING: QtGui.QColor('#4d8fd1'),
    core.STATUS_CANCELLED: QtGui.QColor('#9e9e9e'),
}


def human_size(num) -> str:
    if num is None:
        return '…'
    value = float(num)
    if value < 1024:
        return f'{int(value)} B'
    for unit in ('KiB', 'MiB', 'GiB', 'TiB'):
        value /= 1024.0
        if value < 1024 or unit == 'TiB':
            return f'{value:.2f} {unit}'


def human_duration(seconds) -> str:
    if not seconds:
        return '…'
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


class FlacVerifierWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'flac_verifier'

        self._statuses = {}        # path -> core.STATUS_*
        self._details = {}         # path -> detail string
        self._scan_worker = None
        self._pending_scans = []   # paths queued while a scan is running
        self._verifying = False

        self.controller = core.VerifyController(self)
        self.controller.file_started.connect(self._on_file_started)
        self.controller.file_finished.connect(self._on_file_finished)
        self.controller.progress.connect(self._on_progress)
        self.controller.batch_finished.connect(self._on_batch_finished)

        self._init_ui()
        self._load_settings()

    # ----- UI setup -----

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # Top row: batch actions
        actions_row = QtWidgets.QHBoxLayout()
        self.btn_add_files = QtWidgets.QPushButton('Add Files…')
        self.btn_add_folder = QtWidgets.QPushButton('Add Folder…')
        self.btn_clear = QtWidgets.QPushButton('Clear')
        self.btn_verify = QtWidgets.QPushButton('Verify All')
        self.btn_stop = QtWidgets.QPushButton('Stop')
        self.btn_stop.setEnabled(False)
        self.summary_label = QtWidgets.QLabel('Drop FLAC files or folders here.')

        self.btn_add_files.clicked.connect(self._pick_files)
        self.btn_add_folder.clicked.connect(self._pick_folder)
        self.btn_clear.clicked.connect(self._clear_all)
        self.btn_verify.clicked.connect(self.start_verification)
        self.btn_stop.clicked.connect(self._stop_verification)

        actions_row.addWidget(self.btn_add_files)
        actions_row.addWidget(self.btn_add_folder)
        actions_row.addWidget(self.btn_clear)
        actions_row.addSpacing(20)
        actions_row.addWidget(self.btn_verify)
        actions_row.addWidget(self.btn_stop)
        actions_row.addStretch(1)
        actions_row.addWidget(self.summary_label)
        layout.addLayout(actions_row)

        # Settings row
        settings_row = QtWidgets.QHBoxLayout()
        settings_row.addWidget(QtWidgets.QLabel('flac binary:'))
        self.flac_path_edit = QtWidgets.QLineEdit()
        self.flac_path_edit.setMaximumWidth(280)
        btn_browse_flac = QtWidgets.QPushButton('Browse…')
        btn_browse_flac.clicked.connect(self._pick_flac_binary)
        settings_row.addWidget(self.flac_path_edit)
        settings_row.addWidget(btn_browse_flac)
        settings_row.addSpacing(20)
        settings_row.addWidget(QtWidgets.QLabel('Parallel jobs:'))
        self.jobs_spin = QtWidgets.QSpinBox()
        self.jobs_spin.setRange(0, 64)
        self.jobs_spin.setSpecialValueText('Auto')
        settings_row.addWidget(self.jobs_spin)
        settings_row.addSpacing(20)
        self.recurse_check = QtWidgets.QCheckBox('Recurse into subfolders')
        settings_row.addWidget(self.recurse_check)
        settings_row.addStretch(1)
        layout.addLayout(settings_row)

        # Center: folder-grouped tree + log in a splitter
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        self.tree = BatchFileTree(COLUMNS)
        self.tree.paths_dropped.connect(self.add_paths)
        self.tree.files_removed.connect(self._on_files_removed)
        self.tree.itemDoubleClicked.connect(self._show_details)
        self.tree.setColumnWidth(COL_FILE, 380)
        self.tree.setColumnWidth(COL_SIZE, 90)
        self.tree.setColumnWidth(COL_SPECS, 150)
        self.tree.setColumnWidth(COL_DURATION, 80)
        self.tree.setColumnWidth(COL_MD5, 250)
        self.tree.setColumnWidth(COL_STATUS, 110)
        splitter.addWidget(self.tree)

        log_container = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat('%v / %m files')
        self.progress_bar.setVisible(False)
        log_layout.addWidget(self.progress_bar)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        log_layout.addWidget(self.log)
        splitter.addWidget(log_container)

        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

    # ----- settings -----

    def _load_settings(self):
        settings = self.app_manager.load_config(self.tool_name, config.DEFAULTS)
        self.flac_path_edit.setText(settings.get('flac_path', config.DEFAULTS['flac_path']))
        self.jobs_spin.setValue(int(settings.get('parallel_jobs', config.DEFAULTS['parallel_jobs'])))
        self.recurse_check.setChecked(bool(settings.get('recursive_scan', config.DEFAULTS['recursive_scan'])))

    def save_settings(self):
        self.app_manager.save_config(self.tool_name, {
            'flac_path': self.flac_path_edit.text().strip() or 'flac',
            'parallel_jobs': self.jobs_spin.value(),
            'recursive_scan': self.recurse_check.isChecked(),
        })

    # ----- adding files -----

    def _pick_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, 'Add FLAC files', '', 'FLAC files (*.flac);;All files (*)')
        if files:
            self.add_paths(files)

    def _pick_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, 'Add folder')
        if folder:
            self.add_paths([folder])

    def _pick_flac_binary(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Select flac binary')
        if path:
            self.flac_path_edit.setText(path)

    def add_paths(self, paths):
        """Entry point for drops and Add buttons; scans happen off-thread."""
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._pending_scans.extend(paths)
            return
        self._start_scan(paths)

    def _start_scan(self, paths):
        self.btn_verify.setEnabled(False)
        self._scan_worker = core.ScanWorker(paths, recursive=self.recurse_check.isChecked(), parent=self)
        self._scan_worker.groups_ready.connect(self._on_groups_ready)
        self._scan_worker.meta_ready.connect(self._on_meta_ready)
        self._scan_worker.scan_failed.connect(self._on_scan_failed)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.start()

    def _on_groups_ready(self, groups):
        added = 0
        for folder, files in groups.items():
            for path in files:
                item = self.tree.add_file(folder, path, [
                    os.path.basename(path), '…', '…', '…', '…',
                    core.STATUS_LABELS[core.STATUS_PENDING], '',
                ])
                if item is not None:
                    self._statuses[path] = core.STATUS_PENDING
                    added += 1
            self._update_folder_summary(folder)
        if added:
            self._log(f'Added {added} file(s) across {len(groups)} folder(s).')
        else:
            self._log('No new FLAC files found in the dropped paths.')
        self._update_summary_label()

    def _on_meta_ready(self, path, meta):
        if not self.tree.has_file(path):
            return
        if meta.get('error'):
            self._set_status(path, core.STATUS_ERROR, f"Cannot read metadata: {meta['error']}")
        specs = '…'
        if meta.get('sample_rate'):
            specs = f"{meta['sample_rate']} Hz / {meta['bits']}-bit / {meta['channels']}ch"
        self.tree.set_file_texts(path, {
            COL_SIZE: human_size(meta.get('size')),
            COL_SPECS: specs,
            COL_DURATION: human_duration(meta.get('duration')),
            COL_MD5: meta['md5'] if meta.get('md5') else '(not set)',
        })

    def _on_scan_failed(self, message):
        self._log(f'Scan error: {message}')

    def _on_scan_finished(self):
        if self._pending_scans:
            paths, self._pending_scans = self._pending_scans, []
            self._start_scan(paths)
            return
        self._scan_worker = None
        if not self._verifying:
            self.btn_verify.setEnabled(True)
        self._log(f'Ready: {self.tree.folder_count()} folder(s), {self.tree.file_count()} file(s).')

    # ----- verification -----

    def start_verification(self):
        files = self.tree.all_files()
        if not files:
            self._log('Nothing to verify — add some FLAC files first.')
            return
        if self.controller.is_running:
            return

        flac_path = self.flac_path_edit.text().strip() or 'flac'
        if shutil.which(flac_path) is None and not os.path.isfile(flac_path):
            QtWidgets.QMessageBox.warning(
                self, 'flac not found',
                f"The flac binary was not found: '{flac_path}'\n\n"
                'Install flac (e.g. sudo apt install flac) or set the path above.')
            return

        for path in files:
            self._set_status(path, core.STATUS_PENDING, '')

        self._verifying = True
        self.btn_verify.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_clear.setEnabled(False)
        self.tree.set_removal_allowed(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(files))
        self.progress_bar.setValue(0)

        jobs = self.jobs_spin.value()
        self._log(f'Verifying {len(files)} file(s) with `{flac_path} -t` '
                  f'({jobs if jobs > 0 else "auto"} parallel jobs)…')
        self.controller.start(files, flac_path=flac_path, jobs=jobs)

    def _stop_verification(self):
        if self.controller.is_running:
            self._log('Stopping — terminating running checks…')
            self.controller.cancel()
            self.btn_stop.setEnabled(False)

    def _on_file_started(self, path):
        self._set_status(path, core.STATUS_CHECKING, '')

    def _on_file_finished(self, path, status, detail):
        self._set_status(path, status, detail)
        if status in (core.STATUS_FAILED, core.STATUS_ERROR):
            self._log(f'{core.STATUS_LABELS[status].upper()}: {path}' + (f' — {detail}' if detail else ''))
        elif status == core.STATUS_OK_NO_MD5:
            self._log(f'No MD5 in header (CRCs OK): {path}')

    def _on_progress(self, done, total):
        self.progress_bar.setValue(done)

    def _on_batch_finished(self, counts):
        self._verifying = False
        self.btn_verify.setEnabled(self._scan_worker is None)
        self.btn_stop.setEnabled(False)
        self.btn_clear.setEnabled(True)
        self.tree.set_removal_allowed(True)

        parts = []
        for status in (core.STATUS_OK, core.STATUS_OK_NO_MD5, core.STATUS_FAILED,
                       core.STATUS_ERROR, core.STATUS_CANCELLED):
            if counts.get(status):
                parts.append(f'{counts[status]} {core.STATUS_LABELS[status]}')
        self._log('Batch finished: ' + (', '.join(parts) if parts else 'nothing checked') + '.')
        bad = counts.get(core.STATUS_FAILED, 0) + counts.get(core.STATUS_ERROR, 0)
        if bad:
            self._log(f'*** {bad} file(s) need attention — double-click a row for full details. ***')
        self._update_summary_label()

    # ----- row/status bookkeeping -----

    def _set_status(self, path, status, detail):
        self._statuses[path] = status
        self._details[path] = detail
        self.tree.set_file_texts(path, {
            COL_STATUS: core.STATUS_LABELS[status],
            COL_DETAILS: detail,
        })
        self.tree.set_file_color(path, STATUS_COLORS.get(status))
        folder = os.path.dirname(path)
        self._update_folder_summary(folder)
        self._update_summary_label()

    def _folder_counts(self, folder):
        counts = {}
        for path in self.tree.files_in_folder(folder):
            status = self._statuses.get(path, core.STATUS_PENDING)
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _update_folder_summary(self, folder):
        files = self.tree.files_in_folder(folder)
        if not files:
            return
        counts = self._folder_counts(folder)
        total = len(files)
        ok = counts.get(core.STATUS_OK, 0)
        no_md5 = counts.get(core.STATUS_OK_NO_MD5, 0)
        failed = counts.get(core.STATUS_FAILED, 0) + counts.get(core.STATUS_ERROR, 0)
        checked = ok + no_md5 + failed + counts.get(core.STATUS_CANCELLED, 0)

        if checked == 0:
            text = f'{total} files'
        else:
            bits = []
            if ok:
                bits.append(f'{ok} OK')
            if no_md5:
                bits.append(f'{no_md5} no-MD5')
            if failed:
                bits.append(f'{failed} FAILED')
            text = f'{checked}/{total}: ' + ', '.join(bits) if bits else f'{checked}/{total}'
        self.tree.set_folder_text(folder, COL_STATUS, text)

        item = self.tree.folder_item(folder)
        if item is not None:
            if failed:
                color = STATUS_COLORS[core.STATUS_FAILED]
            elif checked == total and no_md5:
                color = STATUS_COLORS[core.STATUS_OK_NO_MD5]
            elif checked == total and total > 0:
                color = STATUS_COLORS[core.STATUS_OK]
            else:
                color = None
            brush = QtGui.QBrush(color) if color is not None else QtGui.QBrush()
            item.setForeground(COL_STATUS, brush)

    def _update_summary_label(self):
        total = self.tree.file_count()
        if total == 0:
            self.summary_label.setText('Drop FLAC files or folders here.')
            return
        counts = {}
        for status in self._statuses.values():
            counts[status] = counts.get(status, 0) + 1
        bits = [f'{self.tree.folder_count()} folder(s)', f'{total} file(s)']
        for status in (core.STATUS_OK, core.STATUS_OK_NO_MD5, core.STATUS_FAILED, core.STATUS_ERROR):
            if counts.get(status):
                bits.append(f'{counts[status]} {core.STATUS_LABELS[status]}')
        self.summary_label.setText(' · '.join(bits))

    def _on_files_removed(self, paths):
        for path in paths:
            self._statuses.pop(path, None)
            self._details.pop(path, None)
        for folder in self.tree.all_folders():
            self._update_folder_summary(folder)
        self._update_summary_label()

    def _clear_all(self):
        if self._verifying:
            return
        removed = self.tree.clear_all()
        if removed:
            self._statuses.clear()
            self._details.clear()
            self._update_summary_label()
            self._log('Cleared.')

    def _show_details(self, item, column):
        path = item.data(0, BatchFileTree.PATH_ROLE)
        if not path or not self.tree.has_file(path):
            return
        status = self._statuses.get(path, core.STATUS_PENDING)
        detail = self._details.get(path, '')
        QtWidgets.QMessageBox.information(
            self, os.path.basename(path),
            f'{path}\n\nStatus: {core.STATUS_LABELS[status]}'
            + (f'\n\n{detail}' if detail else ''))

    def _log(self, message):
        self.log.appendPlainText(message)

    # ----- lifecycle -----

    def shutdown(self):
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.requestInterruption()
            self._scan_worker.wait(3000)
        if self.controller.is_running:
            self.controller.cancel()
        self.controller.wait(5000)
