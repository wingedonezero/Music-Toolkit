# music_toolkit/tools/flac_verifier/flac_verifier_core.py
#
# Verification runs the reference decoder (`flac -t`): it decodes every frame,
# checks the per-frame CRCs, and compares the decoded audio against the MD5
# stored in STREAMINFO. flac 1.x behavior (calibrated against flac 1.5.0):
#   - success: exit 0, silent
#   - unset MD5: exit 0 + "WARNING, cannot check MD5 signature since it was
#     unset in the STREAMINFO" (frame CRCs are still checked)
#   - damage: exit != 0 + "ERROR ..." lines on stderr

import os
import subprocess
import threading

from PySide6 import QtCore
from mutagen.flac import FLAC

from music_toolkit.core.scanner import scan_paths

STATUS_PENDING = 'pending'
STATUS_CHECKING = 'checking'
STATUS_OK = 'ok'
STATUS_OK_NO_MD5 = 'ok_no_md5'
STATUS_FAILED = 'failed'
STATUS_ERROR = 'error'
STATUS_CANCELLED = 'cancelled'

STATUS_LABELS = {
    STATUS_PENDING: 'Pending',
    STATUS_CHECKING: 'Checking…',
    STATUS_OK: 'OK',
    STATUS_OK_NO_MD5: 'OK (no MD5)',
    STATUS_FAILED: 'FAILED',
    STATUS_ERROR: 'Error',
    STATUS_CANCELLED: 'Cancelled',
}

# Statuses that mean "this file has been processed in the current batch".
FINISHED_STATUSES = {STATUS_OK, STATUS_OK_NO_MD5, STATUS_FAILED, STATUS_ERROR, STATUS_CANCELLED}

VERIFY_TIMEOUT_S = 600


def read_flac_meta(path: str) -> dict:
    """Reads STREAMINFO fields for the table without decoding any audio."""
    meta = {
        'size': None, 'sample_rate': None, 'bits': None, 'channels': None,
        'duration': None, 'md5': None, 'error': None,
    }
    try:
        meta['size'] = os.path.getsize(path)
        info = FLAC(path).info
        meta['sample_rate'] = info.sample_rate
        meta['bits'] = info.bits_per_sample
        meta['channels'] = info.channels
        meta['duration'] = info.length
        md5 = getattr(info, 'md5_signature', 0) or 0
        meta['md5'] = f'{md5:032x}' if md5 else None
    except Exception as e:
        meta['error'] = str(e) or e.__class__.__name__
    return meta


def classify_result(path: str, returncode: int, stderr: str):
    """Maps flac -t output to a (status, detail) pair."""
    name = os.path.basename(path)
    lines = []
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Drop the multi-line "rerun with -F" advice flac prints for non-FLAC input
        if line.startswith(('The input file is either', 'convinced it is a FLAC', '-F parameter')):
            continue
        if line.startswith(name + ':'):
            line = line[len(name) + 1:].strip()
        if line and line not in lines:
            lines.append(line)
    detail = ' | '.join(lines)

    if returncode == 0:
        if 'cannot check MD5' in stderr:
            return STATUS_OK_NO_MD5, 'No MD5 in STREAMINFO (frame CRCs verified)'
        return STATUS_OK, detail
    return STATUS_FAILED, detail or f'flac exited with code {returncode}'


class ScanWorker(QtCore.QThread):
    """Walks dropped paths and reads metadata off the GUI thread."""

    groups_ready = QtCore.Signal(object)      # {folder: [file, ...]}
    meta_ready = QtCore.Signal(str, object)   # path, meta dict
    scan_failed = QtCore.Signal(str)

    def __init__(self, paths, recursive=True, parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self._recursive = recursive

    def run(self):
        try:
            groups = scan_paths(self._paths, {'.flac'}, recursive=self._recursive)
            self.groups_ready.emit(groups)
            for files in groups.values():
                for path in files:
                    if self.isInterruptionRequested():
                        return
                    self.meta_ready.emit(path, read_flac_meta(path))
        except Exception as e:
            self.scan_failed.emit(str(e))


class _VerifyTask(QtCore.QRunnable):
    def __init__(self, controller, path):
        super().__init__()
        self._controller = controller
        self._path = path

    def run(self):
        self._controller._run_one(self._path)


class VerifyController(QtCore.QObject):
    """Runs `flac -t` over a batch with a bounded worker pool."""

    file_started = QtCore.Signal(str)
    file_finished = QtCore.Signal(str, str, str)   # path, status, detail
    batch_finished = QtCore.Signal(object)         # {status: count}
    progress = QtCore.Signal(int, int)             # done, total

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        self._lock = threading.Lock()
        self._procs = {}
        self._cancelled = threading.Event()
        self._counts = {}
        self._done = 0
        self._total = 0
        self._flac_path = 'flac'
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, files, flac_path='flac', jobs=0) -> bool:
        if self._running or not files:
            return False
        self._flac_path = flac_path
        self._cancelled.clear()
        self._procs.clear()
        self._counts = {}
        self._done = 0
        self._total = len(files)
        self._running = True
        if jobs <= 0:
            jobs = max(1, QtCore.QThread.idealThreadCount())
        self._pool.setMaxThreadCount(jobs)
        for path in files:
            self._pool.start(_VerifyTask(self, path))
        return True

    def cancel(self):
        """Skips queued files and terminates the flac processes in flight."""
        self._cancelled.set()
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass

    def wait(self, ms=5000) -> bool:
        return self._pool.waitForDone(ms)

    # ----- worker-thread side -----

    def _run_one(self, path):
        if self._cancelled.is_set():
            self._report(path, STATUS_CANCELLED, '')
            return
        self.file_started.emit(path)
        try:
            proc = subprocess.Popen(
                [self._flac_path, '-t', '-s', path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors='replace',
            )
        except FileNotFoundError:
            self._report(path, STATUS_ERROR, f"flac binary not found: '{self._flac_path}'")
            return
        except OSError as e:
            self._report(path, STATUS_ERROR, str(e))
            return

        with self._lock:
            self._procs[path] = proc
        try:
            _, stderr = proc.communicate(timeout=VERIFY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self._report(path, STATUS_ERROR, f'verification timed out after {VERIFY_TIMEOUT_S}s')
            return
        finally:
            with self._lock:
                self._procs.pop(path, None)

        if self._cancelled.is_set() and proc.returncode != 0:
            # Process was likely terminated by cancel(); don't report it as damage.
            self._report(path, STATUS_CANCELLED, '')
            return
        status, detail = classify_result(path, proc.returncode, stderr)
        self._report(path, status, detail)

    def _report(self, path, status, detail):
        with self._lock:
            self._counts[status] = self._counts.get(status, 0) + 1
            self._done += 1
            done, total = self._done, self._total
            finished = done >= total
            counts = dict(self._counts)
        self.file_finished.emit(path, status, detail)
        self.progress.emit(done, total)
        if finished:
            self._running = False
            self.batch_finished.emit(counts)
