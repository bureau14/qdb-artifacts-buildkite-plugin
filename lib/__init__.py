"""Build artifacts in an S3-compatible store: upload, download, promote.

The library has two callers: the plugin's hooks, through `cli`, and a program
outside Buildkite that imports the names below to find and fetch what a
download would. Both resolve "which build holds this variant" through the same
functions, independent of Buildkite hooks.

Each module has one duty and imports only modules listed above it, so that a
module can be read, and tested, with nothing below it:

    errors      how the library fails
    log         one line to the job log, and sizes as the log shows them
    retry       repeat a call with backoff
    config      StoreConfig: which store, read from the environment, then SSM
    clients     boto3 clients for that store, and the credentials they use
    layout      every object key the plugin reads or writes
    objects     small objects in the store, one request each
    rules       file patterns: "archive!entries", excludes
    manifest    manifest.json of a build: write, read
    resolve     which build a download gets: the pointer, the walk over refs
    extract     unpack an archive, refusing tar members that leave the target
    annotation  the list of uploaded files on the build page
    upload      select files, transfer, manifest, annotation
    download    resolve, transfer, extract
    promote     move LATEST_SUCCESSFUL to a build
    options     the plugin's options as a job sees them in its environment
    cli         the command line the hooks call
"""

from .clients import ObjectAuth, aws_clients, resolve_object_auth
from .config import StoreConfig, load_store_config
from .download import download_artifacts
from .resolve import ResolvedArtifact, ResolvedBuild, resolve_artifacts

__all__ = [
    "ObjectAuth",
    "ResolvedArtifact",
    "ResolvedBuild",
    "StoreConfig",
    "aws_clients",
    "download_artifacts",
    "load_store_config",
    "resolve_artifacts",
    "resolve_object_auth",
]
