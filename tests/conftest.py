"""The suite's two worlds, each a module with its fixtures: ``world``, what a job of the
plugin runs in, and ``chain``, a chain of builds played in process over the fakes of the
planning half (``snapshot/``)."""

pytest_plugins = ("world", "chain")
