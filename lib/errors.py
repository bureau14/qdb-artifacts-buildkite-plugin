"""The one error the library raises for a failure it can explain."""


class ArtifactsError(Exception):
    """A missing option, an unresolvable build, an empty selection, a store configuration
    the plugin cannot use: its message is for people. The command line prints it as
    "[artifacts] ERROR: <message>" and exits 1."""
