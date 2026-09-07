"""The scheduler: what runs, when, and what it recorded.

Five modules, split by what changes for what reason. `jobs.py` is the fixed
set of commands and the queue each waits in; `runs.py` is the
`scheduler_runs` lifecycle; `service.py` is the process that owns
APScheduler and the subprocesses.

APScheduler is imported inside functions, never at module level. It is the
`[scheduler]` extra, and `yfin --help` must not require it.
"""

from __future__ import annotations

from yfin.scheduler.jobs import JOBS, JOBS_BY_NAME, Job

__all__ = ["JOBS", "JOBS_BY_NAME", "Job"]
