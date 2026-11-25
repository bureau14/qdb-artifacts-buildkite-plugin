"""Build artifacts in an S3-compatible store: upload, download, promote.

The library has two callers: the plugin's hooks, through `cli`, and a program
outside Buildkite that imports the names below to find and fetch what a
download would: one step of a platform or the promoted steps of a snapshot. Both
resolve "which key holds this step of this platform" through the same
functions. `planning/` plans snapshot dependencies; only the plan step and
the last step import it.

A failure the library can explain raises ArtifactsError. Other failures raise
the exception of boto3, of zstandard or of the standard library.
"""

from .clients import ssm_client
from .config import ObjectAuth, StoreConfig, load_store_config, resolve_object_auth
from .download import download_artifacts
from .errors import ArtifactsError
from .manifest import read_manifest
from .resolve import Branch, Key, ResolvedArtifact, ResolvedBuild, ResolvedStep, resolve_artifacts

__all__ = [
    "ArtifactsError",
    "Branch",
    "Key",
    "ObjectAuth",
    "ResolvedArtifact",
    "ResolvedBuild",
    "ResolvedStep",
    "StoreConfig",
    "download_artifacts",
    "load_store_config",
    "read_manifest",
    "resolve_artifacts",
    "resolve_object_auth",
    "ssm_client",
]
