"""How the plan step and the last step fail.

A decision fails its step by raising a ``Refusal``. Failing is: log why; write the failure
record the refusal names; show why on the build page; end with ``ArtifactsError``. The cause
is in the log before any of these effects runs, so a failure of one of them never hides it.
Any other exception fails the step the same way and writes no record: an error of the store,
of git or of an API says nothing of the build of a key.
"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import ArtifactsError
from ..job import Job
from .annotation import STEP_ERROR, refusal_annotation
from .edge import Edge, create_record
from .records import Origin, Record, record


class Refusal(Exception):
    """A decision that fails the step: its message, its annotation, and its record, if any."""

    def __init__(self, message: str, annotation: str, record: Record | None = None) -> None:
        super().__init__(message)
        self.annotation = annotation
        self.record = record


def refusal(
    job: Job, edge: Edge, kind: str, message: str, origin: Origin | None = None, **said
) -> Refusal:
    """A refusal of ``kind`` (``annotation.REFUSAL_HEADINGS``) before anything is built. With
    the ``origin`` of the build's records it carries the record of that reason and of what
    else is ``said``."""
    written = None if origin is None else record(origin, edge.clock(), reason=kind, **said)
    return Refusal(message, refusal_annotation(kind, job.project, job.build, message), written)


def run_step(job: Job, edge: Edge, decisions: Callable[[Job, Edge], None], context: str) -> int:
    """Run a step's decisions; a refusal or any exception fails the step, under the
    annotation context ``context``."""
    try:
        decisions(job, edge)
        return 0
    except Refusal as exc:
        failed = exc
    except Exception as exc:  # noqa: BLE001 -- whatever ends the step, it ends as failed
        failed = refusal(job, edge, STEP_ERROR, f"{type(exc).__name__}: {exc}")
    edge.log(str(failed))
    if failed.record is not None:
        create_record(edge, failed.record)
    edge.agent.annotate(failed.annotation, "error", context)
    raise ArtifactsError(str(failed))
