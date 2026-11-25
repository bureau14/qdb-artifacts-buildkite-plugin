"""Lines of the job log, and file sizes the way those lines show them."""

import sys


def log(msg):
    print(f"[artifacts] {msg}", file=sys.stderr)


def fmt_size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"
