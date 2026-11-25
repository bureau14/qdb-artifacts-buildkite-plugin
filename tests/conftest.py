"""The suite's two worlds, each a module with its fixtures: ``world``, what a job of the
plugin runs in, and ``builds``, the builds of a tree played in process over the fakes of the
planning half (``planning/``)."""

pytest_plugins = ("world", "builds")
