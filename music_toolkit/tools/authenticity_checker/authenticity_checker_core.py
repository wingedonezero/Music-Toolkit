# music_toolkit/tools/authenticity_checker/authenticity_checker_core.py
#
# Lossy-transcode ("fake lossless") detection. Design follows auCDtect /
# Lossless Audio Checker and newer research on compression-history detection
# (e.g. Hennequin et al. ICASSP 2017; Koops et al. arXiv:2407.21545): a hard
# spectral cutoff alone is a weak, spoofable feature, so the verdict combines
# four signals measured from a streaming STFT:
#
#   1. effective bandwidth — highest frequency with energy above the midband
#      reference; lossy encoders low-pass (LAME: 128k ~16 kHz, V2 ~19 kHz,
#      320 ~20.5 kHz; ffmpeg AAC/Vorbis similar).
#   2. shelf sharpness — dB drop across the cutoff within ~1 kHz. Encoder
#      low-passes are brickwalls; natural masters roll off gently.
#   3. dead-HF ratio — fraction of active frames whose spectrum above the
#      cutoff is essentially empty. Real CD rips keep a continuous noise/
#      dither floor up to Nyquist; decoded lossy audio has digital silence
#      (with occasional leakage) above the low-pass.
#   4. spectral holes — fraction of near-zero bands between 10 kHz and the
#      cutoff in otherwise active frames (psychoacoustic band zeroing).
#
# Plus two Lossless-Audio-Checker-style container checks:
#   - upscaling: declared bit depth higher than the effective bit depth
#     (e.g. 16-bit audio padded into a 24-bit FLAC).
#   - upsampling: hi-res container whose content stops at a lower Nyquist
#     (e.g. 96 kHz file with a sharp ceiling at 22.05 kHz).
#
# Weights/thresholds below were calibrated against a known-good CDDA album
# and MP3/AAC/Vorbis/Opus transcodes of it round-tripped back to FLAC.

import hashlib
import math
import os
import threading
import time

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC
from PySide6 import QtCore

from music_toolkit.core.config import APP_NAME, APP_VERSION
from music_toolkit.core.scanner import scan_paths

ANALYZER_NAME = f"{APP_NAME} Authenticity Checker"
ANALYZER_VERSION = "1.0.0"

WINDOW = 4096
HOP = 2048
BAND_BINS = 8                      # banded spectrogram resolution (~86 Hz @ 44.1k)
FRAME_GATE_DB = -75.0              # frames quieter than this (midband) are ignored
DEAD_HF_REL_DB = -65.0             # HF region this far under midband counts as dead
HOLE_REL_DB = -60.0                # band this far under midband counts as a hole
BITDEPTH_PROBE_SECONDS = 60

# Verdict scoring (calibrated against true CDDA + generated transcodes; the
# originals in the calibration album only reach ~17-19 kHz themselves, so a
# low ceiling alone must never convict — conviction needs a brickwall and/or
# a dead noise floor above it, which clean masters never showed).
W_BANDWIDTH = 0.45                 # bandwidth evidence (gated by shelf sharpness)
W_SHELF = 0.25
W_DEAD_HF = 0.20
W_HOLES = 0.10
BW_CLEAN = 0.955                   # f_cut / nyquist at or above this scores 0
BW_BAD = 0.70                      # ... at or below this scores 1
SHELF_CLEAN_DB = 10.0
SHELF_BAD_DB = 35.0
SHELF_GATE_DB = 20.0               # bandwidth evidence scales with shelf/this
STRONG_MIN = 0.25                  # brickwall AND dead floor both above this
DEAD_ALONE_MIN = 0.25              # dead floor alone starts to raise suspicion
MPEG_THRESHOLD = 60                # p_lossy >= this -> "MPEG p%"
CDDA_THRESHOLD = 40                # p_lossy <= this -> "CDDA (100-p)%"
UPSAMPLE_MIN_RATE = 88200
UPSAMPLE_MAX_FCUT = 25000.0
UPSAMPLE_MIN_DEAD = 0.4

# Real-vs-fake 24-bit: a 16-bit TPDF dither floor measures ~-93..-96 dBFS in
# the quietest passages; noise-shaped dither lands somewhat higher. A genuine
# 24-bit production floor sits well below that. Between the two windows we
# refuse to guess (e.g. material with no quiet passages, or analog tape noise).
FAKE24_FLOOR_HI = -84.0
FAKE24_FLOOR_LO = -99.0
TRUE24_FLOOR = -105.0

# (low, high, label) — suspected source when a sharp cutoff lands in a range.
# LAME values from the Hydrogenaudio LAME wiki (3.99/3.100 defaults @ 44.1k).
CUTOFF_SIGNATURES = [
    (15500, 16600, 'MP3 ~128 kbps / Vorbis low-q'),
    (16600, 17100, 'MP3 V5/V6 (LAME)'),
    (17100, 17900, 'MP3 V4 (LAME) / AAC ~17 kHz low-pass'),
    (17900, 18600, 'MP3 V3 (LAME)'),
    (18600, 19300, 'MP3 V2 (LAME)'),
    (19300, 20000, 'MP3 V1 (LAME) / AAC high bitrate'),
    (20000, 20700, 'MP3 320/V0 (LAME) / Opus full-band'),
]


class AnalysisCancelled(Exception):
    pass


def _band_db(power):
    return 10.0 * np.log10(np.maximum(power, 1e-30))


def suspect_for_cutoff(f_cut: float) -> str:
    for low, high, label in CUTOFF_SIGNATURES:
        if low <= f_cut < high:
            return label
    return 'unknown lossy encoder'


def compute_spectral_features(path: str, cancel: threading.Event = None) -> dict:
    """Single streaming pass: full-resolution LTAS + banded per-frame powers."""
    win = np.hanning(WINDOW)
    win_norm = np.sum(win ** 2)

    with sf.SoundFile(path) as snd:
        fs = snd.samplerate
        cell = max(1, int(round(fs * 0.1)))
        power_sum = None
        band_frames = []
        raw_cell_ms = []
        tail = np.zeros((0,), dtype=np.float64)
        frames_total = 0

        while True:
            if cancel is not None and cancel.is_set():
                raise AnalysisCancelled()
            chunk = snd.read(1 << 20, dtype='float64', always_2d=True)
            if len(chunk) == 0:
                break
            # 100 ms raw cells (per channel) for the noise-floor analysis;
            # the sub-cell remainder of each chunk is negligible and dropped.
            n_cells = len(chunk) // cell
            if n_cells:
                cells = chunk[:n_cells * cell].reshape(n_cells, cell, chunk.shape[1])
                raw_cell_ms.append(np.mean(cells ** 2, axis=1))
            mono = np.mean(chunk, axis=1) if chunk.shape[1] > 1 else chunk[:, 0]
            buf = np.concatenate([tail, mono])
            if len(buf) < WINDOW:
                tail = buf
                continue
            n_frames = (len(buf) - WINDOW) // HOP + 1
            frames = np.lib.stride_tricks.sliding_window_view(buf, WINDOW)[::HOP][:n_frames]
            spec = np.fft.rfft(frames * win, axis=1)
            power = (np.abs(spec) ** 2) / win_norm
            if power_sum is None:
                power_sum = np.zeros(power.shape[1])
            power_sum += np.sum(power, axis=0)
            frames_total += len(power)
            usable = (power.shape[1] // BAND_BINS) * BAND_BINS
            banded = power[:, :usable].reshape(len(power), -1, BAND_BINS).mean(axis=2)
            band_frames.append(banded.astype(np.float32))
            tail = buf[n_frames * HOP:]

    if power_sum is None or frames_total == 0:
        raise ValueError('audio too short for spectral analysis')

    # Quietest non-silent moment in the file (3rd-smallest cell for robustness
    # against a single glitch cell; exact-zero cells are digital silence and
    # prove nothing about bit depth).
    noise_floor_db = None
    if raw_cell_ms:
        vals = np.concatenate(raw_cell_ms).ravel()
        vals = np.sort(vals[vals > 0])
        if len(vals):
            noise_floor_db = 10.0 * math.log10(float(vals[min(2, len(vals) - 1)]))

    ltas_db = _band_db(power_sum / frames_total)
    bands = np.concatenate(band_frames)
    freqs = np.fft.rfftfreq(WINDOW, 1.0 / fs)
    band_freqs = freqs[:(len(freqs) // BAND_BINS) * BAND_BINS].reshape(-1, BAND_BINS).mean(axis=1)
    features = _features_from_spectra(fs, freqs, ltas_db, band_freqs, bands)
    features['noise_floor_db'] = noise_floor_db
    return features


def _features_from_spectra(fs, freqs, ltas_db, band_freqs, bands) -> dict:
    nyquist = fs / 2.0

    def bin_at(f):
        return int(np.searchsorted(freqs, f))

    # Reference level: midband (1-8 kHz) long-term average
    ref_db = float(np.median(ltas_db[bin_at(1000):max(bin_at(8000), bin_at(1000) + 8)]))

    # Effective cutoff: highest frequency still within 60 dB of the midband.
    # Smoothing pads with edge values (zero-padding in dB would read as loud
    # and pin the cutoff to Nyquist), and the threshold never dips into the
    # file's own noise floor (dither sits ~10+ dB below real HF content).
    kernel = np.ones(5) / 5.0
    smooth = np.convolve(np.pad(ltas_db, 2, mode='edge'), kernel, mode='valid')
    floor_db = float(np.percentile(smooth[bin_at(4000):], 5))
    threshold = max(ref_db - 60.0, floor_db + 12.0)
    above = np.nonzero(smooth >= threshold)[0]
    above = above[(freqs[above] >= 1000)]
    f_cut = float(freqs[above[-1]]) if len(above) else float(nyquist)
    f_cut = min(f_cut, nyquist)

    # Shelf sharpness: level just below the cutoff vs just above it.
    below_lo, below_hi = bin_at(max(f_cut - 1100, 200)), bin_at(max(f_cut - 100, 300))
    above_lo, above_hi = bin_at(min(f_cut + 100, nyquist)), bin_at(min(f_cut + 1100, nyquist))
    if above_hi - above_lo >= 3 and below_hi - below_lo >= 3:
        shelf_drop = float(np.mean(ltas_db[below_lo:below_hi]) - np.mean(ltas_db[above_lo:above_hi]))
    else:
        shelf_drop = 0.0   # cutoff is at/near Nyquist; no shelf to measure
    shelf_drop = max(0.0, shelf_drop)

    # Per-frame stats from the banded spectrogram
    bands_db = _band_db(bands)
    mid = (band_freqs >= 1000) & (band_freqs <= 8000)
    frame_mid_db = bands_db[:, mid].mean(axis=1)
    active = frame_mid_db > FRAME_GATE_DB

    hf = band_freqs > min(f_cut + 500, nyquist - 200)
    if np.any(hf) and np.any(active) and f_cut < nyquist * 0.99:
        hf_db = _band_db(bands[:, hf].mean(axis=1))
        dead_hf_ratio = float(np.mean(hf_db[active] < frame_mid_db[active] + DEAD_HF_REL_DB))
    else:
        dead_hf_ratio = 0.0

    hole_region = (band_freqs >= 10000) & (band_freqs <= f_cut - 1000)
    if np.any(hole_region) and np.any(active):
        region_db = bands_db[:, hole_region]
        holes = region_db[active] < (frame_mid_db[active, None] + HOLE_REL_DB)
        holes_ratio = float(np.mean(holes))
    else:
        holes_ratio = 0.0

    return {
        'sample_rate': fs,
        'f_cut': f_cut,
        'bw_ratio': f_cut / nyquist,
        'shelf_drop': shelf_drop,
        'dead_hf_ratio': dead_hf_ratio,
        'holes_ratio': holes_ratio,
        'active_frames': int(np.sum(active)),
    }


def check_bit_depth(path: str, declared_bits: int) -> dict:
    """LAC-style upscale check: effective vs declared bit depth."""
    result = {'effective_bits': declared_bits, 'upscaled': False}
    try:
        with sf.SoundFile(path) as snd:
            data = snd.read(min(len(snd), snd.samplerate * BITDEPTH_PROBE_SECONDS),
                            dtype='int32', always_2d=True)
        nonzero = data[data != 0]
        if len(nonzero) == 0:
            return result
        # libsndfile left-justifies into int32; common trailing zeros beyond
        # the container shift mean the low bits of the declared depth are padding.
        tz = 32
        for shift in range(0, 32, 4):
            mask = (1 << (shift + 4)) - 1
            partial = nonzero & mask
            if np.any(partial):
                low = nonzero & ((1 << shift) - 1) if shift else None
                for bit in range(shift, shift + 4):
                    if np.any(nonzero & (1 << bit)):
                        tz = bit
                        break
                break
        effective = 32 - tz
        result['effective_bits'] = min(effective, declared_bits)
        result['upscaled'] = effective < declared_bits and declared_bits > 16 and effective <= 16
    except Exception:
        pass
    return result


def score_verdict(features: dict, bits_info: dict) -> dict:
    fs = features['sample_rate']
    nyquist = fs / 2.0
    f_cut = features['f_cut']
    shelf = features['shelf_drop']
    dead = features['dead_hf_ratio']

    # Hi-res container whose content stops near CD Nyquist with nothing but
    # digital silence above it = upsampled. (The empty top octave also trips
    # the dead-HF feature, so lossy evidence from it is suppressed below.)
    upsampled = bool(fs >= UPSAMPLE_MIN_RATE and f_cut <= UPSAMPLE_MAX_FCUT
                     and dead > UPSAMPLE_MIN_DEAD)

    s_shelf = min(max((shelf - SHELF_CLEAN_DB) / (SHELF_BAD_DB - SHELF_CLEAN_DB), 0.0), 1.0)
    s_dead = min(max(dead, 0.0), 1.0)
    s_holes = min(features['holes_ratio'] / 0.25, 1.0)

    # Bandwidth evidence, judged against the plausible source rate and gated
    # by shelf sharpness: a low ceiling with a gentle slope is just an old
    # master, not a transcode.
    reference_nyquist = min(nyquist, 22050.0) if fs >= UPSAMPLE_MIN_RATE else nyquist
    bw_ratio = min(f_cut / reference_nyquist, 1.0)
    s_bw = min(max((BW_CLEAN - bw_ratio) / (BW_CLEAN - BW_BAD), 0.0), 1.0)
    s_bw_gated = s_bw * min(max(shelf / SHELF_GATE_DB, 0.0), 1.0)

    # Genuine hi-res content (ceiling well above any lossy codec's low-pass,
    # e.g. 44 kHz in a 96k container): the natural rolloff toward Nyquist looks
    # like a brickwall with silence above, but no codec produces that band —
    # treating it as lossy evidence would convict every real 96k master.
    hires_genuine = bool(fs >= UPSAMPLE_MIN_RATE and f_cut > UPSAMPLE_MAX_FCUT)

    if upsampled or hires_genuine:
        # The shelf/dead-floor features measure the empty top octave, not
        # lossy coding; only the in-band hole evidence remains usable.
        candidates = [W_HOLES * s_holes]
    else:
        candidates = [W_BANDWIDTH * s_bw_gated + W_SHELF * s_shelf
                      + W_DEAD_HF * s_dead + W_HOLES * s_holes]
        # A brickwall with a dead floor above it is conclusive on its own,
        # wherever the cutoff sits (this is what catches 320/V0-grade MP3).
        strong = min(s_shelf, s_dead)
        if strong >= STRONG_MIN:
            candidates.append(0.55 + 0.45 * math.sqrt(strong))
        # A substantially dead floor above the ceiling alone (no sharp shelf
        # measured) still warrants suspicion — clean masters measured 0.00.
        dead_alone = min(max((dead - DEAD_ALONE_MIN) / 0.5, 0.0), 1.0)
        if dead_alone > 0:
            candidates.append(0.40 + 0.25 * dead_alone)

    p_lossy = min(max(100.0 * max(candidates), 0.0), 100.0)

    if p_lossy >= MPEG_THRESHOLD:
        conclusion = f'MPEG {p_lossy:.0f}%'
        kind = 'lossy'
    elif p_lossy <= CDDA_THRESHOLD:
        conclusion = f'CDDA {100 - p_lossy:.0f}%'
        kind = 'clean'
    else:
        conclusion = f'CDDA {100 - p_lossy:.0f}% (uncertain)'
        kind = 'uncertain'

    flags = []
    bit_assessment = ''
    declared = bits_info.get('declared_bits', 16)
    floor_db = features.get('noise_floor_db')
    if bits_info.get('upscaled'):
        flags.append(f"upscaled: zero-padded {bits_info['effective_bits']}-bit "
                     f"in a {declared}-bit container")
    elif declared >= 24 and floor_db is not None:
        if FAKE24_FLOOR_LO <= floor_db <= FAKE24_FLOOR_HI:
            flags.append(f'suspect {declared}-bit: noise floor {floor_db:.0f} dBFS '
                         f'≈ 16-bit source')
        elif floor_db < TRUE24_FLOOR:
            bit_assessment = (f'noise floor {floor_db:.0f} dBFS — consistent with '
                              f'true {declared}-bit')
        else:
            bit_assessment = f'noise floor {floor_db:.0f} dBFS — bit depth indeterminate'
    if upsampled:
        flags.append(f'upsampled (content stops at {f_cut / 1000:.1f} kHz)')

    suspect = suspect_for_cutoff(f_cut) if kind == 'lossy' else ''
    return {
        'p_lossy': p_lossy, 'conclusion': conclusion, 'kind': kind,
        'flags': flags, 'suspect': suspect, 'upsampled': upsampled,
        'bit_assessment': bit_assessment, 'noise_floor_db': floor_db,
    }


def analyze_track(path: str, cancel: threading.Event = None,
                  compute_signature: bool = True) -> dict:
    result = {
        'path': path, 'error': None, 'size': None, 'duration': None,
        'md5': '', 'sha1': '', 'features': None, 'verdict': None,
        'conclusion': '', 'flags': [], 'suspect': '', 'kind': '',
        'bits_declared': None, 'bits_effective': None,
        'noise_floor_db': None, 'bit_assessment': '',
    }
    try:
        result['size'] = os.path.getsize(path)
        declared_bits = None
        try:
            info = FLAC(path).info
            declared_bits = info.bits_per_sample
            result['duration'] = info.length
            md5 = getattr(info, 'md5_signature', 0) or 0
            result['md5'] = f'{md5:032X}' if md5 else ''
        except Exception:
            pass

        features = compute_spectral_features(path, cancel=cancel)
        result['features'] = features
        if result['duration'] is None:
            result['duration'] = 0.0

        bits_info = check_bit_depth(path, declared_bits or 16)
        bits_info['declared_bits'] = declared_bits or 16

        verdict = score_verdict(features, bits_info)
        result['verdict'] = verdict
        result['conclusion'] = verdict['conclusion']
        result['flags'] = verdict['flags']
        result['suspect'] = verdict['suspect']
        result['kind'] = verdict['kind']
        result['bits_declared'] = bits_info['declared_bits']
        result['bits_effective'] = bits_info.get('effective_bits')
        result['noise_floor_db'] = verdict.get('noise_floor_db')
        result['bit_assessment'] = verdict.get('bit_assessment', '')

        if compute_signature:
            if cancel is not None and cancel.is_set():
                raise AnalysisCancelled()
            sha1 = hashlib.sha1()
            with open(path, 'rb') as f:
                for block in iter(lambda: f.read(1 << 20), b''):
                    sha1.update(block)
            result['sha1'] = sha1.hexdigest().upper()
    except AnalysisCancelled:
        raise
    except Exception as e:
        result['error'] = str(e) or e.__class__.__name__
    return result


# ----- report rendering (auCDtect Task Manager layout, CRLF, UTF-8) -----

def render_report(tracks: list, log_date: str = None) -> str:
    if log_date is None:
        log_date = time.strftime('%Y-%m-%d %H:%M:%S')
    banner = '- - - - - - - - - - - - - - - - - - - - - - -'
    lines = [
        banner,
        '',
        "DON'T MODIFY THIS FILE",
        '',
        banner,
        '',
        f'PERFORMER: {ANALYZER_NAME}, ver. {ANALYZER_VERSION}',
        f'log date: {log_date}',
        '',
        f'ANALYZER: {APP_NAME} spectral authenticity analysis, version {APP_VERSION}',
        'Hash: FLAC STREAMINFO audio MD5. Signature: SHA-1 of the file.',
        '',
        '',
    ]
    for t in tracks:
        name = os.path.basename(t['path'])
        lines.append(f'FILE: {name}')
        if t['error'] is not None:
            lines.append(f"    Error: {t['error']}")
            continue
        lines.append(f"    Size: {t['size']} Hash: {t['md5'] or '-'} Accuracy: standard")
        conclusion = t['conclusion']
        extras = list(t['flags'])
        if t['suspect']:
            extras.append(f"suspect: {t['suspect']}")
        if extras:
            conclusion += ' [' + '; '.join(extras) + ']'
        lines.append(f'    Conclusion: {conclusion}')
        lines.append(f"    Signature: {t['sha1'] or '-'}")
    lines.append('')
    return '\r\n'.join(lines) + '\r\n'


def write_report(folder: str, tracks: list, filename: str) -> str:
    report_path = os.path.join(folder, filename)
    with open(report_path, 'w', encoding='utf-8', newline='') as f:
        f.write(render_report(tracks))
    return report_path


# ----- Qt orchestration (same shape as the DR meter) -----

class ScanWorker(QtCore.QThread):
    groups_ready = QtCore.Signal(object)
    meta_ready = QtCore.Signal(str, object)
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


class AuthenticityController(QtCore.QObject):
    album_started = QtCore.Signal(str)
    track_done = QtCore.Signal(str, object)
    album_done = QtCore.Signal(str, object, str)   # folder, [tracks], report path
    album_failed = QtCore.Signal(str, str)
    progress = QtCore.Signal(int, int)
    batch_finished = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._running = False
        self._write_reports = True
        self._report_filename = 'Folder.auCDtect.txt'
        self._albums_left = 0
        self._tracks_done = 0
        self._tracks_total = 0
        self._error_count = 0
        self._lossy_count = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, groups: dict, jobs=0, write_reports=True,
              report_filename='Folder.auCDtect.txt') -> bool:
        if self._running or not groups:
            return False
        self._cancel.clear()
        self._write_reports = write_reports
        self._report_filename = report_filename
        self._albums_left = len(groups)
        self._tracks_done = 0
        self._tracks_total = sum(len(f) for f in groups.values())
        self._error_count = 0
        self._lossy_count = 0
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

    def _run_album(self, folder, files):
        try:
            if self._cancel.is_set():
                raise AnalysisCancelled()
            self.album_started.emit(folder)

            tracks = []
            for path in files:
                if self._cancel.is_set():
                    raise AnalysisCancelled()
                track = analyze_track(path, cancel=self._cancel,
                                      compute_signature=self._write_reports)
                tracks.append(track)
                with self._lock:
                    self._tracks_done += 1
                    done, total = self._tracks_done, self._tracks_total
                    if track['error'] is not None:
                        self._error_count += 1
                    elif track['kind'] == 'lossy':
                        self._lossy_count += 1
                self.track_done.emit(folder, track)
                self.progress.emit(done, total)

            report_path = ''
            if self._write_reports and any(t['error'] is None for t in tracks):
                try:
                    report_path = write_report(folder, tracks, self._report_filename)
                except OSError as e:
                    self.album_failed.emit(folder, f'could not write report: {e}')
            self.album_done.emit(folder, tracks, report_path)
        except AnalysisCancelled:
            self.album_failed.emit(folder, 'cancelled')
        except Exception as e:
            self.album_failed.emit(folder, str(e) or e.__class__.__name__)
        finally:
            with self._lock:
                self._albums_left -= 1
                finished = self._albums_left <= 0
                summary = {'errors': self._error_count, 'lossy': self._lossy_count,
                           'cancelled': self._cancel.is_set()}
            if finished:
                self._running = False
                self.batch_finished.emit(summary)
