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


def parse_rules(patterns):
    """Parse pattern strings into (archive_glob, entry_filter, strip_prefix) tuples.

    Format: ``archive_glob[!entry_filter]``
      archive_glob  -- fnmatch against S3 key filename (e.g. '*-server.tar.zst')
      entry_filter  -- optional fnmatch against archive entries (e.g. 'bin/*')
      strip_prefix  -- fixed prefix before first glob char, stripped from output paths
                      so 'bin/qdb' extracts to output_dir/qdb, not output_dir/bin/qdb
    """
    rules = []
    for pat in patterns:
        if "!" in pat:
            archive_glob, entry_filter = pat.split("!", 1)
            prefix_end = len(entry_filter)
            for i, ch in enumerate(entry_filter):
                if ch in ("*", "?", "["):
                    prefix_end = i
                    break
            strip_prefix = entry_filter[:prefix_end]
            rules.append((archive_glob, entry_filter, strip_prefix))
        else:
            rules.append((pat, None, None))
    return rules


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


def _select_entry_path(name, entry_rules):
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
