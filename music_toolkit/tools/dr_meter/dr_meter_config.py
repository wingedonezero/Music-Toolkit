# music_toolkit/tools/dr_meter/dr_meter_config.py

DEFAULTS = {
    "parallel_albums": 0,        # 0 = auto (one album per CPU core)
    "write_reports": True,       # write a foo_dr.txt into each analyzed folder
    "report_filename": "foo_dr.txt",
    "append_loudness": True,     # EBU R128 section after the classic DR block
    "recursive_scan": True,
}
