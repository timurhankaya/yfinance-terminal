#!/bin/sh
# One entrypoint for every service. Its single job: empty
# PROMETHEUS_MULTIPROC_DIR (set only on `api`) before uvicorn starts, or
# mmap files from dead workers are summed into every value after a restart.
# `exec` so the real process is PID 1 and gets SIGTERM directly.
set -e

if [ -n "${PROMETHEUS_MULTIPROC_DIR}" ]; then
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
    # Only the library's own files, in case the directory holds anything else.
    find "${PROMETHEUS_MULTIPROC_DIR}" -maxdepth 1 -name '*.db' -delete
fi

exec "$@"
