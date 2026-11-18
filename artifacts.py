#!/usr/bin/env python3
"""Command line of the plugin, as the hooks start it.

This file sits at the plugin root, outside the package, on purpose. Python puts
the directory of the script it runs first on sys.path, so `lib` imports here as
a package, exactly as it does for an outside caller of the library, and the
modules inside `lib` can import each other relatively. The hooks need no
PYTHONPATH, so nothing depends on how Git Bash translates path lists on Windows.
"""

from lib.artifacts import main

if __name__ == "__main__":
    raise SystemExit(main())
