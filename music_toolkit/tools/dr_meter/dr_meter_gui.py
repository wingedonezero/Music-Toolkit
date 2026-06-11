# music_toolkit/tools/dr_meter/dr_meter_gui.py

import os

from PySide6 import QtWidgets, QtCore, QtGui

from music_toolkit.gui.batch_tree import BatchFileTree
from . import dr_meter_config as config
from . import dr_meter_core as core

COLUMNS = ['File', 'Duration', 'DR', 'Peak', 'RMS', 'LUFS', 'LRA', 'True Peak', 'Status']
(COL_FILE, COL_DURATION, COL_DR, COL_PEAK, COL_RMS,
 COL_LUFS, COL_LRA, COL_TP, COL_STATUS) = range(len(COLUMNS))


def fmt_or_dash(value, fmt='{:.1f}'):
    return fmt.format(value) if value is not None else '—'

COLOR_DONE = QtGui.QColor('#3fa34d')
COLOR_RUNNING = QtGui.QColor('#4d8fd1')
COLOR_ERROR = QtGui.QColor('#e05252')
COLOR_CANCELLED = QtGui.QColor('#9e9e9e')


class DrMeterWidget(QtWidgets.QWidget):
    def __init__(self, app_manager, parent=None):
        super().__init__(parent)
        self.app_manager = app_manager
        self.tool_name = 'dr_meter'

        self._scan_worker = None
        self._pending_scans = []
        self._analyzing = False

        self.controller = core.DrController(self)
        self.controller.album_started.connect(self._on_album_started)
        self.controller.track_done.connect(self._on_track_done)
        self.controller.album_done.connect(self._on_album_done)
        self.controller.album_failed.connect(self._on_album_failed)
        self.controller.progress.connect(self._on_progress)
        self.controller.batch_finished.connect(self._on_batch_finished)

        self._init_ui()
        self._load_settings()

    # ----- UI setup -----

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        actions_row = QtWidgets.QHBoxLayout()
        self.btn_add_files = QtWidgets.QPushButton('Add Files…')
        self.btn_add_folder = QtWidgets.QPushButton('Add Folder…')
        self.btn_clear = QtWidgets.QPushButton('Clear')
        self.btn_analyze = QtWidgets.QPushButton('Analyze All')
        self.btn_stop = QtWidgets.QPushButton('Stop')
        self.btn_stop.setEnabled(False)
        self.summary_label = QtWidgets.QLabel('Drop FLAC files or folders here — each folder is measured as its own album.')

        self.btn_add_files.clicked.connect(self._pick_files)
        self.btn_add_folder.clicked.connect(self._pick_folder)
        self.btn_clear.clicked.connect(self._clear_all)
        self.btn_analyze.clicked.connect(self.start_analysis)
        self.btn_stop.clicked.connect(self._stop_analysis)

        actions_row.addWidget(self.btn_add_files)
        actions_row.addWidget(self.btn_add_folder)
        actions_row.addWidget(self.btn_clear)
        actions_row.addSpacing(20)
        actions_row.addWidget(self.btn_analyze)
        actions_row.addWidget(self.btn_stop)
        actions_row.addStretch(1)
        actions_row.addWidget(self.summary_label)
        layout.addLayout(actions_row)

        settings_row = QtWidgets.QHBoxLayout()
        self.write_reports_check = QtWidgets.QCheckBox('Write foo_dr.txt into each folder')
        settings_row.addWidget(self.write_reports_check)
        settings_row.addSpacing(20)
        self.loudness_check = QtWidgets.QCheckBox('Append EBU R128 section to report')
        settings_row.addWidget(self.loudness_check)
        settings_row.addSpacing(20)
        settings_row.addWidget(QtWidgets.QLabel('Parallel albums:'))
        self.jobs_spin = QtWidgets.QSpinBox()
        self.jobs_spin.setRange(0, 64)
        self.jobs_spin.setSpecialValueText('Auto')
        settings_row.addWidget(self.jobs_spin)
        settings_row.addSpacing(20)
        self.recurse_check = QtWidgets.QCheckBox('Recurse into subfolders')
        settings_row.addWidget(self.recurse_check)
        settings_row.addStretch(1)
        layout.addLayout(settings_row)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        self.tree = BatchFileTree(COLUMNS)
        self.tree.paths_dropped.connect(self.add_paths)
        self.tree.setColumnWidth(COL_FILE, 360)
        self.tree.setColumnWidth(COL_DURATION, 80)
        self.tree.setColumnWidth(COL_DR, 60)
        self.tree.setColumnWidth(COL_PEAK, 90)
        self.tree.setColumnWidth(COL_RMS, 90)
        self.tree.setColumnWidth(COL_LUFS, 90)
        self.tree.setColumnWidth(COL_LRA, 70)
        self.tree.setColumnWidth(COL_TP, 90)
        splitter.addWidget(self.tree)

        log_container = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat('%v / %m tracks')
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
        self.jobs_spin.setValue(int(settings.get('parallel_albums', config.DEFAULTS['parallel_albums'])))
        self.write_reports_check.setChecked(bool(settings.get('write_reports', config.DEFAULTS['write_reports'])))
        self.loudness_check.setChecked(bool(settings.get('append_loudness', config.DEFAULTS['append_loudness'])))
        self.recurse_check.setChecked(bool(settings.get('recursive_scan', config.DEFAULTS['recursive_scan'])))
        self._report_filename = settings.get('report_filename', config.DEFAULTS['report_filename'])

    def save_settings(self):
        self.app_manager.save_config(self.tool_name, {
            'parallel_albums': self.jobs_spin.value(),
            'write_reports': self.write_reports_check.isChecked(),
            'append_loudness': self.loudness_check.isChecked(),
            'report_filename': self._report_filename,
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

    def add_paths(self, paths):
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._pending_scans.extend(paths)
            return
        self._start_scan(paths)

    def _start_scan(self, paths):
        self.btn_analyze.setEnabled(False)
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
                    os.path.basename(path), '…', '', '', '', 'Pending'])
                if item is not None:
                    added += 1
            self.tree.set_folder_text(folder, COL_STATUS, f'{len(self.tree.files_in_folder(folder))} files')
        if added:
            self._log(f'Added {added} file(s) across {len(groups)} folder(s).')
        else:
            self._log('No new FLAC files found in the dropped paths.')
        self._update_summary()

    def _on_meta_ready(self, path, meta):
        if meta.get('duration'):
            self.tree.set_file_texts(path, {COL_DURATION: core.format_duration(meta['duration'])})

    def _on_scan_failed(self, message):
        self._log(f'Scan error: {message}')

    def _on_scan_finished(self):
        if self._pending_scans:
            paths, self._pending_scans = self._pending_scans, []
            self._start_scan(paths)
            return
        self._scan_worker = None
        if not self._analyzing:
            self.btn_analyze.setEnabled(True)
        self._log(f'Ready: {self.tree.folder_count()} folder(s), {self.tree.file_count()} file(s).')

    # ----- analysis -----

    def start_analysis(self):
        groups = {folder: self.tree.files_in_folder(folder) for folder in self.tree.all_folders()}
        groups = {f: files for f, files in groups.items() if files}
        if not groups:
            self._log('Nothing to analyze — add some FLAC files first.')
            return
        if self.controller.is_running:
            return

        total = sum(len(f) for f in groups.values())
        for folder, files in groups.items():
            self.tree.set_folder_text(folder, COL_DR, '')
            self.tree.set_folder_text(folder, COL_LUFS, '')
            self.tree.set_folder_text(folder, COL_LRA, '')
            self.tree.set_folder_text(folder, COL_STATUS, 'Queued')
            for path in files:
                self.tree.set_file_texts(path, {COL_DR: '', COL_PEAK: '', COL_RMS: '',
                                                COL_LUFS: '', COL_LRA: '', COL_TP: '',
                                                COL_STATUS: 'Pending'})
                self.tree.set_file_color(path, None)

        self._analyzing = True
        self.btn_analyze.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_clear.setEnabled(False)
        self.tree.set_removal_allowed(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)

        jobs = self.jobs_spin.value()
        write_reports = self.write_reports_check.isChecked()
        self._log(f'Analyzing {total} track(s) in {len(groups)} album(s) '
                  f'({jobs if jobs > 0 else "auto"} parallel albums, '
                  f'reports {"on" if write_reports else "off"})…')
        self.controller.start(groups, jobs=jobs, write_reports=write_reports,
                              report_filename=self._report_filename,
                              append_loudness=self.loudness_check.isChecked())

    def _stop_analysis(self):
        if self.controller.is_running:
            self._log('Stopping after the current blocks finish…')
            self.controller.cancel()
            self.btn_stop.setEnabled(False)

    def _on_album_started(self, folder):
        self.tree.set_folder_text(folder, COL_STATUS, 'Analyzing…')
        item = self.tree.folder_item(folder)
        if item is not None:
            item.setForeground(COL_STATUS, QtGui.QBrush(COLOR_RUNNING))

    def _on_track_done(self, folder, track):
        path = track['path']
        if not self.tree.has_file(path):
            return
        if track['error'] is not None:
            self.tree.set_file_texts(path, {COL_STATUS: f"Error: {track['error']}"})
            self.tree.set_file_color(path, COLOR_ERROR)
            self._log(f"Error: {path} — {track['error']}")
            return
        self.tree.set_file_texts(path, {
            COL_DURATION: core.format_duration(track['duration']),
            COL_DR: f"DR{track['dr']}",
            COL_PEAK: f"{core.format_db(track['peak_db'])} dB",
            COL_RMS: f"{core.format_db(track['rms_db'])} dB",
            COL_LUFS: f"{fmt_or_dash(track['lufs'])} LUFS",
            COL_LRA: f"{fmt_or_dash(track['lra'])} LU",
            COL_TP: f"{fmt_or_dash(track['tp_db'], '{:+.1f}')} dBTP",
            COL_STATUS: 'Done',
        })
        self.tree.set_file_color(path, COLOR_DONE)

    def _on_album_done(self, folder, album, report_path):
        if album['album_dr'] is None:
            self.tree.set_folder_text(folder, COL_STATUS, 'no readable tracks')
            self._set_folder_color(folder, COLOR_ERROR)
            return
        self.tree.set_folder_text(folder, COL_DR, f"DR{album['album_dr']}")
        self.tree.set_folder_text(folder, COL_LUFS, f"{fmt_or_dash(album['album_lufs'])} LUFS")
        self.tree.set_folder_text(folder, COL_LRA, f"{fmt_or_dash(album['album_lra'])} LU")
        status = 'Done'
        if report_path:
            status = f'Done — wrote {os.path.basename(report_path)}'
        elif album['errors']:
            status = f"Done — {album['errors']} track error(s)"
        self.tree.set_folder_text(folder, COL_STATUS, status)
        self._set_folder_color(folder, COLOR_ERROR if album['errors'] else COLOR_DONE)
        name = f"{album['artist']} / {album['album']}"
        self._log(f"Album DR{album['album_dr']}: {name}"
                  + (f' — report: {report_path}' if report_path else ''))

    def _on_album_failed(self, folder, error):
        if error == 'cancelled':
            self.tree.set_folder_text(folder, COL_STATUS, 'Cancelled')
            self._set_folder_color(folder, COLOR_CANCELLED)
        else:
            self.tree.set_folder_text(folder, COL_STATUS, f'Error: {error}')
            self._set_folder_color(folder, COLOR_ERROR)
            self._log(f'Album failed: {folder} — {error}')

    def _set_folder_color(self, folder, color):
        item = self.tree.folder_item(folder)
        if item is not None:
            brush = QtGui.QBrush(color) if color is not None else QtGui.QBrush()
            for col in (COL_DR, COL_STATUS):
                item.setForeground(col, brush)

    def _on_progress(self, done, total):
        self.progress_bar.setValue(done)

    def _on_batch_finished(self, info):
        self._analyzing = False
        self.btn_analyze.setEnabled(self._scan_worker is None)
        self.btn_stop.setEnabled(False)
        self.btn_clear.setEnabled(True)
        self.tree.set_removal_allowed(True)
        if info.get('cancelled'):
            self._log('Batch cancelled.')
        else:
            self._log('Batch finished.'
                      + (f" {info['errors']} track error(s)." if info.get('errors') else ''))
        self._update_summary()

    # ----- misc -----

    def _update_summary(self):
        total = self.tree.file_count()
        if total == 0:
            self.summary_label.setText('Drop FLAC files or folders here — each folder is measured as its own album.')
        else:
            self.summary_label.setText(f'{self.tree.folder_count()} folder(s) · {total} file(s)')

    def _clear_all(self):
        if self._analyzing:
            return
        if self.tree.clear_all():
            self._update_summary()
            self._log('Cleared.')

    def _log(self, message):
        self.log.appendPlainText(message)

    # ----- lifecycle -----

    def shutdown(self):
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.requestInterruption()
            self._scan_worker.wait(3000)
        if self.controller.is_running:
            self.controller.cancel()
        self.controller.wait(10000)
