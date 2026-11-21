"""Build artifacts in an S3-compatible store: upload, download, promote.

The library has two callers: the plugin's hooks, through `cli`, and a program
outside Buildkite that imports the names below to find and fetch what a
download would, one step of a platform in a `Branch` or at a `Key`. Both
resolve "which key holds this step of this platform" through the same
functions, independent of Buildkite hooks.

Each module has one duty and imports only modules listed above it, so that a
module can be read, and tested, with nothing below it:

    errors      ArtifactsError, the one error the library raises
    log         one line to the job log, and sizes as the log shows them
    retry       repeat a call with backoff
    config      StoreConfig: which store, read from the environment, then SSM
    clients     boto3 clients for that store, and the credentials they use
    layout      every object key the plugin reads or writes, and the step id
    forms       the forms of a ref and of a time
    key         the key of a build, and key.json, the canonical JSON it is a hash of
    objects     the store of the destination; its small objects: get, put, create once, list
    rules       file patterns: "archive!entries", excludes
    manifest    manifest.json of a step's folder: its files, who uploaded and promoted them
    resolve     which steps a download takes: at a given key, or by the walk over latest files
    transfer    many files at once, each with its own client and retries
    extract     unpack an archive, refusing members that leave the target
    annotation  on the build page, a step's files and the build each download took
    upload      select files, capture them and their manifest, annotate
    download    an entry's steps, their files, transfer, extract, annotate
    promote     promote a step's folder once, and write the branch's latest file
    options     the plugin's options as a job sees them in its environment
    snapshot/   the planning of snapshot dependencies; only the gate steps import it
    cli         the command line the hooks call

A failure the library can explain raises ArtifactsError. Other failures raise
the exception of boto3, of zstandard or of the standard library, and a Ctrl-C
during a transfer ends the process with status 130.
"""

from .clients import ObjectAuth, aws_clients, resolve_object_auth
from .config import StoreConfig, load_store_config
from .download import download_artifacts
from .errors import ArtifactsError
from .manifest import read_manifest
from .resolve import Branch, Key, ResolvedArtifact, ResolvedStep, resolve_artifacts

__all__ = [
    "ArtifactsError",
    "Branch",
    "Key",
    "ObjectAuth",
    "ResolvedArtifact",
    "ResolvedStep",
    "StoreConfig",
    "aws_clients",
    "download_artifacts",
    "load_store_config",
    "read_manifest",
    "resolve_artifacts",
    "resolve_object_auth",
]
