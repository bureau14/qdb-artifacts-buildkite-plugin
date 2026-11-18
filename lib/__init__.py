"""Build artifacts in an S3-compatible store: upload, download, promote.

The library has two callers: the plugin's hooks, through `cli`, and a program
outside Buildkite that imports the names below to find and fetch what a
download would. Both resolve "which build holds this variant" through the same
functions, independent of Buildkite hooks.

Each module has one duty and imports only modules listed above it, so that a
module can be read, and tested, with nothing below it:

    errors      ArtifactsError, the one error the library raises
    log         one line to the job log, and sizes as the log shows them
    retry       repeat a call with backoff
    config      StoreConfig: which store, read from the environment, then SSM
    clients     boto3 clients for that store, and the credentials they use
    layout      every object key the plugin reads or writes
    objects     small objects in the store: get, put, create once, list
    rules       file patterns: "archive!entries", excludes
    manifest    manifest.json of a build: write, read
    resolve     which build a download gets: the pointer, the walk over refs
    transfer    many files at once, each with its own client and retries
    extract     unpack an archive, refusing tar members that leave the target
    annotation  the list of uploaded files on the build page
    upload      select files, transfer, manifest, annotation
    download    resolve, transfer, extract
    pointer     the snapshot pointer: which build holds a variant of a snapshot
    promote     move LATEST_SUCCESSFUL to a build; with a key, create its pointer
    options     the plugin's options as a job sees them in its environment
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
from .resolve import ResolvedArtifact, ResolvedBuild, resolve_artifacts

__all__ = [
    "ArtifactsError",
    "ObjectAuth",
    "ResolvedArtifact",
    "ResolvedBuild",
    "StoreConfig",
    "aws_clients",
    "download_artifacts",
    "load_store_config",
    "read_manifest",
    "resolve_artifacts",
    "resolve_object_auth",
]
