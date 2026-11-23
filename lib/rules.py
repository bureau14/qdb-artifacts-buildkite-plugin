"""File patterns: which artifacts a step selects, and which entries of an archive.

A download pattern is `archive_glob[!entry_filter]`. `*-server.tar.zst!bin/*`
selects the archives matching `*-server.tar.zst`, extracts only their entries
matching `bin/*`, and strips `bin/` from the output paths, so that a test step
takes only the executables and libraries of a component archive.

Exclude patterns drop what the patterns matched, by the artifact's path: on
upload the path below `base_dir`, on download the path in the build. An excluded
file is neither transferred nor listed in the manifest or the annotation.
"""

import fnmatch
import re


def parse_rule(pattern):
    """(archive_glob, entry_filter, strip_prefix) of ``archive_glob[!entry_filter]``.

    archive_glob  -- fnmatch against the artifact's path (e.g. '*-server.tar.zst')
    entry_filter  -- optional fnmatch against archive entries (e.g. 'bin/*')
    strip_prefix  -- the filter's fixed prefix before its first glob character, stripped from
                     output paths, so 'bin/qdb' extracts to output_dir/qdb
    """
    archive_glob, bang, entry_filter = pattern.partition("!")
    if not bang:
        return pattern, None, None
    return archive_glob, entry_filter, re.match(r"[^*?[]*", entry_filter)[0]


def parse_rules(patterns):
    return [parse_rule(p) for p in patterns]


def match_rules(rel, rules):
    """Return every (entry_filter, strip_prefix) rule matching an artifact path.

    Multiple filters may intentionally match one archive. The archive is downloaded
    and decompressed once, while its entries are selected from the union of those
    filters. Rule order resolves overlapping entry filters.
    """
    return [
        (entry_filter, strip_prefix)
        for archive_glob, entry_filter, strip_prefix in rules
        if fnmatch.fnmatch(rel, archive_glob)
    ]


def is_excluded(rel, exclude_patterns):
    """Return True when rel matches any configured exclude archive glob."""
    return any(fnmatch.fnmatch(rel, pat) for pat in exclude_patterns or [])


def select_entry_path(name, entry_rules):
    """Return the output path for the first matching entry rule, or None.

    Rule order only matters for overlapping entry filters. Selecting one path per
    archive entry keeps streamed tar extraction to one decompression pass.
    """
    for entry_filter, strip_prefix in entry_rules:
        if entry_filter and not fnmatch.fnmatch(name, entry_filter):
            continue
        if strip_prefix and name.startswith(strip_prefix):
            name = name[len(strip_prefix) :]
        return name or None
    return None
