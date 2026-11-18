"""Every object key the plugin reads or writes.

    <prefix>/<project>/<git ref>/variants/<variant>/
        LATEST_SUCCESSFUL                 text, a build id; moved by promote
        builds/<build id>/manifest.json   written by upload
        builds/<build id>/<file>          written by upload

The destination names the bucket and the prefix. Project, ref and build id keep
the runs of pipelines apart; the variant keeps apart what the steps of one
build produce, so that a test step downloads under the variant its build step
uploaded to. A file keeps its path relative to the checkout, or to `base_dir`.
"""

from urllib.parse import urlparse


def parse_s3(uri):
    """Parse s3://bucket/prefix URI into (bucket, prefix).
    Also handles plain bucket names (legacy SSM format)."""
    if not uri.startswith("s3://"):
        return uri.strip("/"), ""
    p = urlparse(uri)
    return p.netloc, p.path.strip("/")


def key_join(*parts):
    """Join S3 key segments, dropping blanks to avoid double slashes."""
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


def scope(
    cfg,
    project_id,
    build_id,
    variant,
    git_ref,
    skip_build_id=False,
):
    """Resolve (bucket, key_prefix) for the current build/variant context.
    project_id scopes the artifacts to a specific project.
    variant (--variant) specifies os/arch/etc. to use. Resolved at pipeline-generation time.
    git_ref (--git-ref) specifies the Git reference (branch or tag) to use.
    skip_build_id skips the build ID in the path (used for LATEST_SUCCESSFUL pointers)."""

    bucket, prefix = parse_s3(cfg.destination)

    if skip_build_id:
        return bucket, key_join(prefix, project_id, git_ref, "variants", variant)

    if build_id == "LATEST_SUCCESSFUL":
        return bucket, key_join(
            prefix, project_id, git_ref, "variants", variant, "LATEST_SUCCESSFUL"
        )

    return bucket, key_join(prefix, project_id, git_ref, "variants", variant, "builds", build_id)
