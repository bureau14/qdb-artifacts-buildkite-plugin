#!/usr/bin/env python3
"""Command line of the plugin, as the hooks start it.

This file sits at the plugin root, outside the package, on purpose. Python puts
the directory of the script it runs first on sys.path, so `lib` imports here as
a package, exactly as it does for an outside caller of the library, and the
modules inside `lib` can import each other relatively. The hooks need no
PYTHONPATH, so nothing depends on how Git Bash translates path lists on Windows.
With PYTHONSAFEPATH set (Python 3.11 and later) Python leaves that directory off
sys.path, and a hook that starts this file fails with "No module named 'lib'".
"""

from lib.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
