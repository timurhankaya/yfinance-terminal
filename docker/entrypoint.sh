#!/bin/sh
# One entrypoint for every service the image runs.
#
# It exists for a single job that cannot be done in a Dockerfile: emptying
# PROMETHEUS_MULTIPROC_DIR before uvicorn starts.
#
# `prometheus_client` in multiprocess mode gives every worker a set of mmap
# files named after its PID. Nothing removes them when a worker exits, so
# across restarts the directory fills with files whose processes are long
# gone -- and the MultiProcessCollector reads ALL of them. A counter would
# keep the value a dead worker left, a gauge would report a process that no
# longer exists, and the numbers would drift further from the truth with
# every deploy. Clearing at start-up is what the library's own
# documentation asks for.
#
# The variable is set only on the `api` service, never as an image ENV: it
# is read at IMPORT time, process-wide, so an image-wide value would put
# the scheduler, the stream and both relays into multiprocess mode too --
# where each writes files nobody collects and their `/metrics` goes quiet.
#
# `exec` so the real process becomes PID 1 and receives SIGTERM directly.
# Without it this shell would be PID 1, signals would stop here, and
# `docker stop` would wait out its timeout and then SIGKILL a scheduler
# mid-job.
set -e

if [ -n "${PROMETHEUS_MULTIPROC_DIR}" ]; then
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
    # Only the library's own files. `rm -rf "$dir"/*` would be a wider
    # blast radius than this needs if the variable were ever pointed at a
    # directory that holds something else.
    find "${PROMETHEUS_MULTIPROC_DIR}" -maxdepth 1 -name '*.db' -delete
fi

exec "$@"
