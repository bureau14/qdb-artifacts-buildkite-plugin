"""Snapshot dependencies: the plan step, its second run and the last step of a build.

The jobs that upload, download and promote never import this package. ``plan_step`` and ``last_step``
hold what the steps decide, in order: start reading there.
"""
