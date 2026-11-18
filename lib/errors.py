"""How the library fails: a failure it can explain raises ArtifactsError, whose
message is for people.

The command line turns the error into "[artifacts] ERROR: <message>" and exit
status 1, which the hooks pass on to the job. A program that imports the
library catches the error, or lets it end its own process. Other failures raise
the exception of boto3, of zstandard or of the standard library, and a Ctrl-C
during a transfer ends the process at once with status 130: waiting for the
transfers in flight would hold it for minutes.
"""


class ArtifactsError(Exception):
    """A failure the plugin can explain: a missing option, an unresolvable build,
    an empty selection, a store configuration it cannot use."""
