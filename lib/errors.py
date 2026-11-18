"""How the plugin fails: one error line in the job log, then exit status 1.

The hooks pass that status on, so the job fails with the plugin's own message
as its last error line instead of a traceback.
"""

import sys


def die(msg):
    print(f"[artifacts] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)
