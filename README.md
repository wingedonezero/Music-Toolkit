# Music-Toolkit

A multi-tool desktop app for working with music libraries. PySide6 GUI, pure Python,
structured like Remux-Toolkit: tabbed main window, one folder per tool, per-tool JSON
config in `config/`, venv managed by `setup_env.sh`.

All batch tools share the same folder-grouped workflow: drop files or folders, every
folder (album) is treated as its own unit — multi-disc rips with `CD1/`/`CD2/`
subfolders count as separate albums — and per-folder report files land in each folder.

## Tools

### FLAC Verifier
Drag in folders/files, verify integrity in parallel via the reference decoder
(`flac -t`): decodes every frame, checks frame CRCs and the STREAMINFO MD5.
Distinguishes OK / OK-but-no-MD5-stored / FAILED, with the decoder's error detail
per file.

### DR Meter
TT/Pleasurize dynamic range measurement, writing a `foo_dr.txt` into each album
folder in the classic foobar2000 Dynamic Range Meter 1.1.1 layout (CRLF, same
columns; our own header line). The algorithm was calibrated against a known
foobar-generated log: all per-track DR/Peak/RMS values, durations and the album DR
reproduce byte-for-byte. The Bitrate footer line is computed honestly
(`total bytes * 8 / total duration`) — the original foobar component overstated it.

Alongside DR it measures modern loudness per ITU-R BS.1770-4 / EBU R128 —
integrated LUFS, loudness range (LRA) and 4x-oversampled true peak (dBTP) per
track, plus properly gated album-level LUFS/LRA over the whole program (not a
track average). Validated against ffmpeg's `ebur128` filter (±0.1 LU). An EBU
R128 section is appended to `foo_dr.txt` after the classic block's terminator,
so parsers of the original layout are undisturbed (can be turned off).

### Authenticity Checker
auCDtect-style lossy-transcode detection, writing a `Folder.auCDtect.txt` into each
folder (classic per-file block layout, UTF-8; `Hash` = STREAMINFO audio MD5,
`Signature` = SHA-1 of the file). Verdicts are evidence-based, combining:

- effective bandwidth (gated by shelf sharpness, so band-limited old masters are
  not punished),
- brickwall shelf depth across the cutoff,
- dead-HF ratio (digital silence above the cutoff vs a real noise/dither floor),
- spectral holes below the cutoff,
- LAC-style upscale (16-in-24-bit padding) and upsample (hi-res container, CD-rate
  content) checks,
- real-vs-fake 24-bit analysis: beyond zero-padding, the noise floor of the
  quietest non-silent passages is measured — a floor stuck at ~-96 dBFS in a
  24-bit container is a 16-bit dither floor (flagged as a suspected 16-bit
  source), a floor below -105 dBFS is consistent with true 24-bit, and loud
  wall-to-wall material is honestly reported as indeterminate. Genuine hi-res
  bandwidth (content above 25 kHz) is recognized so real 96k masters' natural
  rolloff is never mistaken for a lossy brickwall.

Calibrated against true CDDA rips plus MP3 (128/V2/V0/320), AAC, Vorbis and Opus
transcodes round-tripped to FLAC: no false accusations on clean files; 128k/V2/320
MP3 and Opus are caught at ~90% confidence; upscales/upsamples are flagged. Known
limitation (state of the art for non-ML detectors): transparent-grade encodes
(AAC 256k, V0/Vorbis q6) of masters that were already band-limited below the
encoder's lowpass can pass as clean or land in the "uncertain" zone.

## Setup & run

```bash
./setup_env.sh      # option 1: Full Setup (Python 3.13 venv + dependencies)
./run.sh            # launch
```

External requirement: `flac` in PATH for the FLAC Verifier (e.g. `sudo apt install flac`).
Dependencies (latest, from requirements.txt): PySide6, mutagen, numpy, soundfile.

## Layout

```
Music-Toolkit.py                  # entry point
setup_env.sh / run.sh             # environment + launcher
music_toolkit/
├── core/                         # AppManager (config/temp), shared folder scanner
├── gui/                          # MainWindow (tabs) + shared BatchFileTree widget
└── tools/<tool>/                 # one folder per tool:
    ├── <tool>_config.py          #   DEFAULTS dict
    ├── <tool>_core.py            #   analysis / workers
    └── <tool>_gui.py             #   QWidget with save_settings()/shutdown()
config/ , temp/                   # created at runtime (per-tool JSON + scratch)
```

To add a new tool: create `music_toolkit/tools/<name>/` with the three modules above,
then register it in `music_toolkit/gui/main_window.py` (import, QAction, menu entry,
`open_<name>` method).
