"""How the library fails: it raises ArtifactsError, whose message is for people.

The command line turns the error into "[artifacts] ERROR: <message>" and exit
status 1, which the hooks pass on to the job. A program that imports the
library catches the error, or lets it end its own process; the library never
ends the process of its caller.
"""


class ArtifactsError(Exception):
    """A failure the plugin can explain: a missing option, an unresolvable build,
    an empty selection, a store configuration it cannot use."""
