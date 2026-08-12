#!/bin/sh
set -eu

if [ "${1:-health}" = "health" ]; then
    if [ "$#" -gt 0 ]; then
        shift
    fi
    exec python /app/health.py "$@"
fi

exec "$@"
