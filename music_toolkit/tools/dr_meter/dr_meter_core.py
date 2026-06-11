# music_toolkit/tools/dr_meter/dr_meter_core.py
#
# TT/Pleasurize "DR" dynamic range measurement, calibrated against the
# foobar2000 Dynamic Range Meter 1.1.1 output for a known album (all 13 track
# DR/Peak/RMS values and the album DR reproduced exactly):
#
#   - blocks of 3 * (fs + 60) samples at 44.1 kHz (the TT meter's quirk,
#     also used by the open-source dr14tmeter reference); 3 * fs otherwise.
#     The partial tail block is included with equal weight.
#   - per-channel block RMS uses the TT convention sqrt(2 * mean(x^2)),
#     so a full-scale sine reads 0 dB.
#   - track DR per channel = -20*log10(rms20 / secondHighestBlockPeak), where
#     rms20 is the quadratic mean of the loudest 20% of blocks; displayed DR
#     is round(mean over channels).
#   - displayed track Peak = highest |sample| in dB (2 decimals).
#   - displayed track RMS = quadratic mean over blocks of the channel-combined
#     block RMS (this is what foobar shows; a plain whole-track RMS is up to
#     0.02 dB off).
#   - album ("Official") DR = round(mean of the integer track DRs).

import math
import os
import threading
import time

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC
from PySide6 import QtCore
from scipy import signal as sp_signal

from music_toolkit.core.config import APP_NAME, APP_VERSION
from music_toolkit.core.scanner import scan_paths

REPORT_TOOL_HEADER = f"{APP_NAME} {APP_VERSION} / DR Meter 1.0.0"

AUDIO_MIN = 1.0 / (2.0 ** 24)
MAX_DYNAMIC_DB = 20.0 * math.log10(2.0 ** 24)


class AnalysisCancelled(Exception):
    pass


def block_samples_for(fs: int) -> int:
    return 3 * (fs + 60) if fs == 44100 else 3 * fs


# ----- EBU R128 / ITU-R BS.1770-4 loudness -----

def _k_weighting_stages(fs: int):
    """The two K-weighting biquads (shelving + RLB high-pass) for any rate.

    Analog prototype parameters reproduce the BS.1770 coefficient tables at
    48 kHz (same derivation pyloudnorm/libebur128 use for arbitrary rates).
    """
    stages = []
    # Stage 1: high-frequency shelving filter
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    b = np.array([a * ((a + 1) + (a - 1) * cos_w0 + 2 * math.sqrt(a) * alpha),
                  -2 * a * ((a - 1) + (a + 1) * cos_w0),
                  a * ((a + 1) + (a - 1) * cos_w0 - 2 * math.sqrt(a) * alpha)])
    a_coef = np.array([(a + 1) - (a - 1) * cos_w0 + 2 * math.sqrt(a) * alpha,
                       2 * ((a - 1) - (a + 1) * cos_w0),
                       (a + 1) - (a - 1) * cos_w0 - 2 * math.sqrt(a) * alpha])
    stages.append((b / a_coef[0], a_coef / a_coef[0]))
    # Stage 2: RLB high-pass
    f0, q = 38.13547087602444, 0.5003270373238773
    w0 = 2.0 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    b = np.array([(1 + cos_w0) / 2.0, -(1 + cos_w0), (1 + cos_w0) / 2.0])
    a_coef = np.array([1 + alpha, -2 * cos_w0, 1 - alpha])
    stages.append((b / a_coef[0], a_coef / a_coef[0]))
    return stages


def integrated_loudness(block_ms: np.ndarray):
    """Gated integrated loudness (LUFS) from 400 ms block mean-squares."""
    if len(block_ms) == 0:
        return None
    lk = -0.691 + 10.0 * np.log10(np.maximum(block_ms, 1e-30))
    gated = block_ms[lk > -70.0]
    if len(gated) == 0:
        return None
    rel_gate = -0.691 + 10.0 * math.log10(float(np.mean(gated))) - 10.0
    gated2 = block_ms[(lk > -70.0) & (lk > rel_gate)]
    if len(gated2) == 0:
        gated2 = gated
    return -0.691 + 10.0 * math.log10(float(np.mean(gated2)))


def loudness_range(st_ms: np.ndarray):
    """EBU Tech 3342 loudness range (LU) from 3 s short-term mean-squares."""
    if len(st_ms) == 0:
        return None
    st = -0.691 + 10.0 * np.log10(np.maximum(st_ms, 1e-30))
    abs_gated = st_ms[st > -70.0]
    if len(abs_gated) == 0:
        return None
    rel_gate = -0.691 + 10.0 * math.log10(float(np.mean(abs_gated))) - 20.0
    gated = st[(st > -70.0) & (st > rel_gate)]
    if len(gated) < 2:
        return 0.0
    return float(np.percentile(gated, 95) - np.percentile(gated, 10))


class LoudnessMeter:
    """Streaming BS.1770-4 meter fed by the DR meter's decode loop."""

    TP_CARRY = 64  # samples of context kept around chunk edges for oversampling

    def __init__(self, fs: int, channels: int):
        self.fs = fs
        self.ch = channels
        self.cell = max(1, int(round(fs * 0.1)))   # 100 ms grid
        self._stages = _k_weighting_stages(fs)
        self._zi = [[np.zeros(2) for _ in range(channels)] for _ in self._stages]
        self._kbuf = np.zeros((0, channels))
        self._cells = []
        self._tp_carry = np.zeros((0, channels))
        self._tp_max = 0.0

    def process(self, chunk: np.ndarray):
        # True peak: 4x oversample with carried edge context so every sample
        # is evaluated exactly once with enough filter history.
        x = np.vstack([self._tp_carry, chunk])
        if len(x) > self.TP_CARRY:
            up = sp_signal.resample_poly(x, 4, 1, axis=0, padtype='line')
            lo = (len(self._tp_carry) // 2) * 4
            hi = (len(x) - self.TP_CARRY // 2) * 4
            if hi > lo:
                self._tp_max = max(self._tp_max, float(np.max(np.abs(up[lo:hi]))))
            self._tp_carry = x[-self.TP_CARRY:]
        else:
            self._tp_carry = x

        # K-weighting with persistent per-channel filter state
        filtered = np.empty_like(chunk)
        for c in range(self.ch):
            y = chunk[:, c]
            for s, (b, a) in enumerate(self._stages):
                y, self._zi[s][c] = sp_signal.lfilter(b, a, y, zi=self._zi[s][c])
            filtered[:, c] = y
        buf = np.vstack([self._kbuf, filtered])
        n_cells = len(buf) // self.cell
        if n_cells:
            cells = buf[:n_cells * self.cell].reshape(n_cells, self.cell, self.ch)
            self._cells.append(np.mean(cells ** 2, axis=1))
        self._kbuf = buf[n_cells * self.cell:]

    def finish(self) -> dict:
        # flush the true-peak carry
        if len(self._tp_carry):
            up = sp_signal.resample_poly(self._tp_carry, 4, 1, axis=0, padtype='line')
            lo = (len(self._tp_carry) // 2) * 4 if len(self._tp_carry) > self.TP_CARRY else 0
            self._tp_max = max(self._tp_max, float(np.max(np.abs(up[lo:]))))

        result = {'lufs': None, 'lra': None, 'tp_db': None,
                  'block_ms': np.zeros(0), 'st_ms': np.zeros(0)}
        if self._tp_max > 0:
            result['tp_db'] = 20.0 * math.log10(self._tp_max)
        if not self._cells:
            return result

        cells = np.concatenate(self._cells)            # (n, ch) K-weighted MS
        weights = np.ones(self.ch)
        if self.ch > 3:
            weights[3:] = 1.41
        cell_w = cells @ weights                       # channel-weighted sum
        if len(cell_w) >= 4:                           # 400 ms blocks, 100 ms hop
            result['block_ms'] = np.convolve(cell_w, np.ones(4) / 4.0, mode='valid')
        if len(cell_w) >= 30:                          # 3 s short-term, 100 ms hop
            result['st_ms'] = np.convolve(cell_w, np.ones(30) / 30.0, mode='valid')
        result['lufs'] = integrated_loudness(result['block_ms'])
        result['lra'] = loudness_range(result['st_ms'])
        return result


def analyze_track(path: str, cancel: threading.Event = None) -> dict:
    """Streams a FLAC in DR blocks and computes track DR / Peak / RMS."""
    result = {
        'path': path, 'error': None, 'dr_float': None, 'dr': None,
        'peak_db': None, 'rms_db': None, 'duration': 0.0,
        'sample_rate': None, 'channels': None, 'bits': None, 'size': None,
        'name': os.path.splitext(os.path.basename(path))[0],
        'artist': '', 'album': '',
        'lufs': None, 'lra': None, 'tp_db': None,
        '_block_ms': np.zeros(0), '_st_ms': np.zeros(0),
    }
    try:
        result['size'] = os.path.getsize(path)
        _read_tags(path, result)

        with sf.SoundFile(path) as snd:
            fs = snd.samplerate
            ch = snd.channels
            result['sample_rate'] = fs
            result['channels'] = ch
            bs = block_samples_for(fs)
            meter = LoudnessMeter(fs, ch)

            rms_blocks = []     # per block: per-channel TT RMS
            peak_blocks = []    # per block: per-channel peak
            comb_sq = []        # per block: channel-combined TT RMS squared
            global_peak = 0.0
            frames = 0

            while True:
                if cancel is not None and cancel.is_set():
                    raise AnalysisCancelled()
                blk = snd.read(bs, dtype='float64', always_2d=True)
                if len(blk) == 0:
                    break
                frames += len(blk)
                meter.process(blk)
                mean_sq = np.mean(blk ** 2, axis=0)            # per channel
                rms_blocks.append(np.sqrt(2.0 * mean_sq))
                peak_blocks.append(np.max(np.abs(blk), axis=0))
                comb_sq.append(float(np.mean(2.0 * mean_sq)))  # combined channels
                global_peak = max(global_peak, float(peak_blocks[-1].max()))

        if frames == 0:
            result['error'] = 'empty audio stream'
            return result

        result['duration'] = frames / fs

        rms = np.sort(np.vstack(rms_blocks), axis=0)
        peaks = np.sort(np.vstack(peak_blocks), axis=0)
        seg = rms.shape[0]
        n_blk = max(1, int(math.floor(seg * 0.2)))
        rms20 = np.sqrt(np.sum(rms[seg - n_blk:] ** 2, axis=0) / n_blk)
        second_peak = peaks[seg - 2] if seg >= 2 else peaks[-1]

        dr_ch = np.zeros(ch)
        for c in range(ch):
            if rms20[c] > AUDIO_MIN and second_peak[c] > 0:
                v = -20.0 * math.log10(rms20[c] / second_peak[c])
                dr_ch[c] = v if abs(v) <= MAX_DYNAMIC_DB else 0.0
        result['dr_float'] = float(np.mean(dr_ch))
        result['dr'] = int(round(result['dr_float']))

        result['peak_db'] = 20.0 * math.log10(max(global_peak, AUDIO_MIN))
        result['rms_db'] = 20.0 * math.log10(max(math.sqrt(float(np.mean(comb_sq))), AUDIO_MIN))

        loudness = meter.finish()
        result['lufs'] = loudness['lufs']
        result['lra'] = loudness['lra']
        result['tp_db'] = loudness['tp_db']
        result['_block_ms'] = loudness['block_ms']
        result['_st_ms'] = loudness['st_ms']
    except AnalysisCancelled:
        raise
    except Exception as e:
        result['error'] = str(e) or e.__class__.__name__
    return result


def _read_tags(path: str, result: dict):
    try:
        tags = FLAC(path)
        result['bits'] = tags.info.bits_per_sample

        def first(key):
            values = tags.get(key)
            return str(values[0]).strip() if values else ''

        result['artist'] = first('albumartist') or first('artist')
        result['album'] = first('album')
        title = first('title')
        track_no = first('tracknumber').split('/')[0]
        if title:
            if track_no.isdigit():
                result['name'] = f'{int(track_no):02d}-{title}'
            else:
                result['name'] = title
    except Exception:
        pass  # tags are cosmetic; analysis carries on with filename fallbacks


def analyze_album(folder: str, files: list, cancel: threading.Event = None,
                  track_callback=None) -> dict:
    """Analyzes every track of one folder (album); per-folder semantics."""
    tracks = []
    for path in files:
        if cancel is not None and cancel.is_set():
            raise AnalysisCancelled()
        track = analyze_track(path, cancel=cancel)
        tracks.append(track)
        if track_callback is not None:
            track_callback(track)

    good = [t for t in tracks if t['error'] is None]
    album = {
        'folder': folder, 'tracks': tracks, 'album_dr': None,
        'artist': '', 'album': '', 'samplerate': '', 'channels': '',
        'bits': '', 'bitrate_kbps': None, 'errors': len(tracks) - len(good),
        'album_lufs': None, 'album_lra': None,
    }
    if not good:
        return album

    album['album_dr'] = int(round(sum(t['dr'] for t in good) / len(good)))

    # Album loudness: gate over the concatenated program, per BS.1770/EBU R128
    # (an album is one program; this is not an average of the track values).
    all_blocks = np.concatenate([t['_block_ms'] for t in good]) if good else np.zeros(0)
    all_st = np.concatenate([t['_st_ms'] for t in good]) if good else np.zeros(0)
    album['album_lufs'] = integrated_loudness(all_blocks)
    album['album_lra'] = loudness_range(all_st)

    def consensus(key, fallback=''):
        values = [t[key] for t in good if t[key]]
        if not values:
            return fallback
        unique = sorted(set(values), key=values.index)
        return unique[0] if len(unique) == 1 else ' / '.join(str(u) for u in unique)

    album['artist'] = consensus('artist', os.path.basename(folder))
    album['album'] = consensus('album', os.path.basename(folder))
    album['samplerate'] = consensus('sample_rate')
    album['channels'] = consensus('channels')
    album['bits'] = consensus('bits')

    total_bytes = sum(t['size'] for t in good if t['size'])
    total_dur = sum(t['duration'] for t in good)
    if total_dur > 0:
        album['bitrate_kbps'] = int(round(total_bytes * 8.0 / total_dur / 1000.0))
    return album


# ----- report rendering (foobar2000 DR log layout, CRLF) -----

def format_db(value: float) -> str:
    text = f'{value:.2f}'
    return '0.00' if text == '-0.00' else text


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    m, s = divmod(total, 60)
    return f'{m}:{s:02d}'


def _fmt_or_dash(value, fmt='{:.1f}'):
    return fmt.format(value) if value is not None else '—'


def render_report(album: dict, log_date: str = None, include_loudness: bool = True) -> str:
    dash = '-' * 80
    if log_date is None:
        log_date = time.strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        REPORT_TOOL_HEADER,
        f'log date: {log_date}',
        '',
        dash,
        f"Analyzed: {album['artist']} / {album['album']}",
        dash,
        '',
        'DR         Peak         RMS     Duration Track',
        dash,
    ]
    for t in album['tracks']:
        if t['error'] is not None:
            lines.append(f"??   [analysis failed: {t['error']}] {t['name']}")
            continue
        peak = format_db(t['peak_db'])
        rms = format_db(t['rms_db'])
        lines.append(
            f"{'DR' + str(t['dr']):<4s}{peak:>11s} dB{rms:>9s} dB"
            f"{format_duration(t['duration']):>10s} {t['name']}"
        )
    lines += [
        dash,
        '',
        f"Number of tracks:  {len(album['tracks'])}",
        f"Official DR value: DR{album['album_dr']}",
        '',
        f"Samplerate:        {album['samplerate']} Hz",
        f"Channels:          {album['channels']}",
        f"Bits per sample:   {album['bits']}",
        f"Bitrate:           {album['bitrate_kbps']} kbps",
        'Codec:             FLAC',
        '=' * 80,
    ]

    if include_loudness:
        # Appended after the classic block's terminator so parsers of the
        # original foobar layout are undisturbed.
        lines += [
            '',
            'Loudness (EBU R128 / ITU-R BS.1770-4)',
            dash,
            '  Integrated      LRA   True Peak Track',
            dash,
        ]
        for t in album['tracks']:
            if t['error'] is not None:
                continue
            lines.append(
                f"{_fmt_or_dash(t['lufs']):>7s} LUFS"
                f"{_fmt_or_dash(t['lra']):>6s} LU"
                f"{_fmt_or_dash(t['tp_db'], '{:+.1f}'):>7s} dBTP"
                f" {t['name']}"
            )
        lines += [
            dash,
            '',
            f"Album integrated:  {_fmt_or_dash(album['album_lufs'])} LUFS",
            f"Album LRA:         {_fmt_or_dash(album['album_lra'])} LU",
            '=' * 80,
        ]

    lines.append('')  # the foobar log ends with a blank line
    return '\r\n'.join(lines) + '\r\n'


def write_report(album: dict, filename: str, include_loudness: bool = True) -> str:
    report_path = os.path.join(album['folder'], filename)
    with open(report_path, 'w', encoding='utf-8', newline='') as f:
        f.write(render_report(album, include_loudness=include_loudness))
    return report_path


# ----- Qt orchestration -----

class ScanWorker(QtCore.QThread):
    """Walks dropped paths and reads light metadata off the GUI thread."""

    groups_ready = QtCore.Signal(object)      # {folder: [file, ...]}
    meta_ready = QtCore.Signal(str, object)   # path, {'duration': float|None}
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
                    meta = {'duration': None}
                    try:
                        meta['duration'] = FLAC(path).info.length
                    except Exception:
                        pass
                    self.meta_ready.emit(path, meta)
        except Exception as e:
            self.scan_failed.emit(str(e))


class _AlbumTask(QtCore.QRunnable):
    def __init__(self, controller, folder, files):
        super().__init__()
        self._controller = controller
        self._folder = folder
        self._files = files

    def run(self):
        self._controller._run_album(self._folder, self._files)


class DrController(QtCore.QObject):
    """Analyzes folders (albums) in parallel; each album is one work unit."""

    album_started = QtCore.Signal(str)
    track_done = QtCore.Signal(str, object)        # folder, track result
    album_done = QtCore.Signal(str, object, str)   # folder, album result, report path ('' if none)
    album_failed = QtCore.Signal(str, str)         # folder, error
    progress = QtCore.Signal(int, int)             # done tracks, total tracks
    batch_finished = QtCore.Signal(object)         # {'albums': n, 'errors': n, 'cancelled': bool}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._running = False
        self._write_reports = True
        self._append_loudness = True
        self._report_filename = 'foo_dr.txt'
        self._albums_left = 0
        self._tracks_done = 0
        self._tracks_total = 0
        self._error_count = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, groups: dict, jobs=0, write_reports=True,
              report_filename='foo_dr.txt', append_loudness=True) -> bool:
        if self._running or not groups:
            return False
        self._cancel.clear()
        self._write_reports = write_reports
        self._append_loudness = append_loudness
        self._report_filename = report_filename
        self._albums_left = len(groups)
        self._tracks_done = 0
        self._tracks_total = sum(len(f) for f in groups.values())
        self._error_count = 0
        self._running = True
        if jobs <= 0:
            jobs = max(1, QtCore.QThread.idealThreadCount())
        self._pool.setMaxThreadCount(jobs)
        for folder, files in groups.items():
            self._pool.start(_AlbumTask(self, folder, files))
        return True

    def cancel(self):
        self._cancel.set()

    def wait(self, ms=10000) -> bool:
        return self._pool.waitForDone(ms)

    # ----- worker-thread side -----

    def _run_album(self, folder, files):
        try:
            if self._cancel.is_set():
                raise AnalysisCancelled()
            self.album_started.emit(folder)

            def on_track(track):
                with self._lock:
                    self._tracks_done += 1
                    done, total = self._tracks_done, self._tracks_total
                    if track['error'] is not None:
                        self._error_count += 1
                self.track_done.emit(folder, track)
                self.progress.emit(done, total)

            album = analyze_album(folder, files, cancel=self._cancel, track_callback=on_track)

            report_path = ''
            if self._write_reports and album['album_dr'] is not None:
                try:
                    report_path = write_report(album, self._report_filename,
                                               include_loudness=self._append_loudness)
                except OSError as e:
                    self.album_failed.emit(folder, f'could not write report: {e}')
            self.album_done.emit(folder, album, report_path)
        except AnalysisCancelled:
            self.album_failed.emit(folder, 'cancelled')
        except Exception as e:
            self.album_failed.emit(folder, str(e) or e.__class__.__name__)
        finally:
            with self._lock:
                self._albums_left -= 1
                finished = self._albums_left <= 0
                errors = self._error_count
            if finished:
                self._running = False
                self.batch_finished.emit({'errors': errors, 'cancelled': self._cancel.is_set()})
