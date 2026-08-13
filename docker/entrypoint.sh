#!/bin/sh
set -eu

case "${1:-health}" in
    health)
        if [ "$#" -gt 0 ]; then shift; fi
        exec python /app/health.py "$@"
        ;;
    dependency-smoke)
        if [ "$#" -gt 0 ]; then shift; fi
        exec python /app/dependency_smoke.py "$@"
        ;;
    gpu-smoke)
        if [ "$#" -gt 0 ]; then shift; fi
        exec python /app/gpu_smoke.py "$@"
        ;;
    native-smoke)
        if [ "$#" -gt 0 ]; then shift; fi
        exec python /app/native_smoke.py "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
