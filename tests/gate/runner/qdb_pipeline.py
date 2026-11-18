"""The runner's ``generate`` for the pipelines of the tests, which the gate's enter step runs.

The runner itself lives in another repository; its ``generate`` prints the
pipeline as PyYAML dumps it, and so does this one.
"""

import sys

import yaml


def run(generate_pipeline):
    if sys.argv[1:] != ["generate"]:
        raise SystemExit(f"the runner of the tests knows only generate, not {sys.argv[1:]}")
    sys.stdout.write(yaml.safe_dump(generate_pipeline()))
