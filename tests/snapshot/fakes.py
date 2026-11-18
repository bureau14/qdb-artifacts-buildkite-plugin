"""In-memory stand-ins for the interfaces of the planning half, for tests.

No network, no subprocess. Each fake refuses what the real one refuses, through
the same check functions of the real module.
"""

from __future__ import annotations

from collections.abc import Mapping

from lib.snapshot.agent import (
    AgentError,
    MetaDataAbsent,
    build_option,
    check_annotation_style,
    check_meta_data_value,
)


class RecordingAgent:
    """``Agent`` in memory that records what a job did to its build.

    ``meta_data``: this build's meta-data, read and written. ``builds``: the
    meta-data of other builds by build id, read only. ``outcomes``: step key ->
    outcome; a step that is not listed has the empty outcome (not finished).
    ``uploads`` and ``annotations`` (``(body, style, context)``) are appended
    in call order.
    """

    def __init__(
        self,
        meta_data: Mapping[str, str] | None = None,
        builds: Mapping[str, Mapping[str, str]] | None = None,
        outcomes: Mapping[str, str] | None = None,
    ) -> None:
        self.meta_data = dict(meta_data or {})
        self.builds = {b: dict(m) for b, m in (builds or {}).items()}
        self.outcomes = dict(outcomes or {})
        self.uploads: list[str] = []
        self.annotations: list[tuple[str, str, str]] = []

    def _data(self, build: str | None) -> Mapping[str, str]:
        build_option(build)
        if build is None:
            return self.meta_data
        if build not in self.builds:
            raise AgentError(f"fake agent: no build {build}")
        return self.builds[build]

    def meta_data_exists(self, key: str, build: str | None = None) -> bool:
        return key in self._data(build)

    def meta_data_get(self, key: str, build: str | None = None) -> str:
        data = self._data(build)
        if key not in data:
            raise MetaDataAbsent(f"meta-data {key} is not set")
        return data[key]

    def meta_data_set(self, key: str, value: str) -> None:
        check_meta_data_value(key, value)
        self.meta_data[key] = value

    def step_outcome(self, key: str) -> str:
        return self.outcomes.get(key, "")

    def pipeline_upload(self, document: str) -> None:
        self.uploads.append(document)

    def annotate(self, body: str, style: str, context: str) -> None:
        check_annotation_style(style)
        self.annotations.append((body, style, context))
