#!/usr/bin/env bash
# Starts the Kev judge server for the clear-head hook. Run it again after a reboot.
#   KEV_PORT   port to listen on, 127.0.0.1 only (default 8009)
#   KEV_MODEL  checkpoint (default jaredpalmer/kev-4b — the 0.8b one was tested and isn't usable)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/kev"
# Not a plain `python -m kev.serve`: on Apple Silicon MLX keeps every freed buffer in an unbounded
# cache, and the server grew from 18 GB at load to 24 GB after ~1300 requests. A 1 GB cache limit
# set before Kev imports MLX keeps it at ~10 GB with no slowdown (1.14 s/claim measured vs 1.5).
# No-op on CUDA/ROCm, where mlx isn't installed.
exec uv run --extra serve python -c "
import runpy, sys
try:
    import mlx.core as mx; mx.set_cache_limit(1 << 30)
except ImportError:
    pass
sys.argv = ['kev.serve', '--run', '${KEV_MODEL:-jaredpalmer/kev-4b}', '--port', '${KEV_PORT:-8009}']
runpy.run_module('kev.serve', run_name='__main__')"
