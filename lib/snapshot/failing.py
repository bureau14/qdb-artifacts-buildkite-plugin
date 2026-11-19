"""How a gate step fails (specification section 7, "fail").

Fail is: log why; write the failure record the decision names, when the build
has a key; show why on the build page; set the mode ``failed``; end with
``ArtifactsError``, which the command line turns into exit status 1. The cause
is in the log before any of these effects runs, so a further failure of one of
them, which then ends the step, never hides it.

A decision fails the step by raising a ``Refusal``. Any other exception fails
it the same way and writes no record: a record saves a waiting build from
compiling in vain, and an error of the store, of git or of an API says nothing
of the build of a key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn, TypeVar

from ..errors import ArtifactsError
from .annotation import STEP_ERROR, refusal_annotation
from .edge import FAILED, MODE, Edge, create_record
from .job import Job
from .records import Record
from .shown import shown_text


class Refusal(Exception):
    """A decision that fails the step: its message, its annotation, and its record, if any."""

    def __init__(self, message: str, annotation: str, record: Record | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.annotation = annotation
        self.record = record


def error_refusal(job: Job, exc: Exception) -> Refusal:
    """The refusal of a step that an error ended: its type and text on the build page, no
    record."""
    message = f"{type(exc).__name__}: {exc}"
    shown = "\n".join(shown_text(line) for line in message.splitlines())
    return Refusal(message, refusal_annotation(STEP_ERROR, job.project, job.build, shown))


def fail(edge: Edge, refusal: Refusal, context: str, mark: bool) -> NoReturn:
    """Log the refusal, write its record, annotate, set the mode when ``mark``, end the step."""
    edge.log(refusal.message)
    if refusal.record is not None:
        create_record(edge, refusal.record)
    edge.agent.annotate(refusal.annotation, "error", context)
    if mark:
        edge.agent.meta_data_set(MODE, FAILED)
    raise ArtifactsError(refusal.message)


# The edge a step's decisions take: ``Edge``, or the plan step's, which reaches further.
AnyEdge = TypeVar("AnyEdge", bound=Edge)


def gated(
    job: Job,
    edge: AnyEdge,
    decisions: Callable[[Job, AnyEdge], None],
    context: str,
    mark: bool,
) -> int:
    """Run a step's decisions; a refusal or any exception fails the step."""
    try:
        decisions(job, edge)
    except Refusal as refusal:
        fail(edge, refusal, context, mark)
    except Exception as exc:  # noqa: BLE001 -- whatever ends a gate step, it ends as failed
        fail(edge, error_refusal(job, exc), context, mark)
    return 0
