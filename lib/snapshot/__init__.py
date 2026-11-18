"""The planning half of snapshot dependencies (story sc-20192).

Ordinary upload and download jobs never import this package. Its modules
import each other and, of the rest of ``lib/``, the forms of values, the
pointer and the library's error (``forms``, ``pointer``, ``errors``), so that
the plan step reads a value by the form the job that wrote it checked.

- ``canonical``: canonical JSON and the key of a snapshot.
"""
