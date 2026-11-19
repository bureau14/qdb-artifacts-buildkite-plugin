"""How a gate step fails (specification section 7, "fail").

Fail is: write the failure record the decision names, when the build has a
key; show why on the build page; set the mode ``failed``; end with
``ArtifactsError``, which the command line turns into exit status 1. A further
failure of one of these effects is logged and added to the message, so that
the step still ends with its own cause.

A decision fails the step by raising a ``Refusal``. An error of the edge fails
it the same way and writes no record: a record saves a waiting build from
compiling in vain, and an error of the store, of git or of an API says
nothing of the build of a key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn, TypeVar

from botocore.exceptions import BotoCoreError, ClientError

from ..errors import ArtifactsError
from .agent import AgentError
from .annotation import STEP_ERROR, refusal_annotation
from .edge import FAILED, MODE, Edge, create_record
from .git import GitError
from .job import Job
from .plan import PlanError
from .records import Record
from .shown import shown_text


class Refusal(Exception):
    """A decision that fails the step: its message, its annotation, and its record, if any."""

    def __init__(self, message: str, annotation: str, record: Record | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.annotation = annotation
        self.record = record


# What the edge raises when it fails, and what the planning package raises for
# a plan read from elsewhere. Enter and resume add the errors of the modules
# that read documents.
EDGE_ERRORS = (ArtifactsError, AgentError, GitError, PlanError, ClientError, BotoCoreError)


def error_refusal(job: Job, error: Exception) -> Refusal:
    """The refusal of a step that an error of the edge ended: no record."""
    message = str(error) or type(error).__name__
    shown = "\n".join(shown_text(line) for line in message.splitlines())
    return Refusal(message, refusal_annotation(STEP_ERROR, job.project, job.build, shown))


def survive(edge: Edge, effect: Callable[[], Any], what: str) -> str:
    """Run one effect of a failing step; a further failure is logged and said, never raised."""
    try:
        effect()
        return ""
    except EDGE_ERRORS as exc:
        edge.log(f"could not {what}: {shown_text(str(exc))}")
        return f"; could not {what}: {shown_text(str(exc))}"


def fail(edge: Edge, refusal: Refusal, context: str, mark: bool) -> NoReturn:
    """Write the refusal's record, annotate, set the mode when ``mark``, and end the step."""
    said = ""
    if refusal.record is not None:
        r = refusal.record
        said += survive(edge, lambda: create_record(edge, r), "write the failure record")
    said += survive(
        edge, lambda: edge.agent.annotate(refusal.annotation, "error", context), "annotate"
    )
    if mark:
        said += survive(edge, lambda: edge.agent.meta_data_set(MODE, FAILED), "set the mode")
    raise ArtifactsError(refusal.message + said)


# The edge a step's decisions take: ``Edge``, or the plan step's, which reaches further.
AnyEdge = TypeVar("AnyEdge", bound=Edge)


def gated(
    job: Job,
    edge: AnyEdge,
    decisions: Callable[[Job, AnyEdge], None],
    errors: tuple[type[BaseException], ...],
    context: str,
    mark: bool,
) -> int:
    """Run a step's decisions; a refusal, or an error of ``errors``, fails the step."""
    try:
        decisions(job, edge)
    except Refusal as refusal:
        fail(edge, refusal, context, mark)
    except errors as exc:
        fail(edge, error_refusal(job, exc), context, mark)
    return 0
