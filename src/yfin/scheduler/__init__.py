"""The scheduler: what runs, when, and what it recorded.

APScheduler is imported inside functions, never at module level: it is
the `[scheduler]` extra, and `yfin --help` must not require it.
"""
