"""
Gunicorn Configuration File
Usage: gunicorn -c gunicorn.conf.py wsgi:app
"""
import os

# Calculate workers based on the CPUs actually available to this process.
# multiprocessing.cpu_count() reports the HOST's core count inside an LXC
# (12 on pve2), which inflated the pool to the clamp of 8 workers and
# overran the container's RAM. sched_getaffinity respects the container's
# cpuset, so this yields the intended (2 * cores) + 1.
cpu_cores = len(os.sched_getaffinity(0))
workers = min(max((2 * cpu_cores) + 1, 2), 8)  # Clamp between 2 and 8
if os.environ.get("RSCP_WORKERS", "").strip().isdigit():
    workers = max(1, int(os.environ["RSCP_WORKERS"]))

# Binding: RSCP_BIND (set in rscp.service / /etc/default/rscp), e.g.
# 127.0.0.1:5000 when only a local cloudflared/reverse proxy should connect.
bind = os.environ.get("RSCP_BIND", "").strip() or "0.0.0.0:5000"

# Timeout for slow requests
timeout = 120

# Logging: stdout/stderr only; under systemd both land in journald
# (journalctl -u rscp).
accesslog = "-"  # stdout
errorlog = "-"   # stderr
loglevel = "info"

# Performance
#
# gthread: multi-process AND multi-threaded. POS peripherals (registers,
# customer displays, scanners) poll every ~1s with cheap single-row reads;
# those ride threads without tying up a whole worker, while CPU-bound work
# (image processing, SQLite) spreads across worker processes/cores. This
# replaced the old single eventlet worker, where any blocking call froze
# every terminal at once. Scheduled jobs run from systemd timers, not in the
# workers, so scaling workers does not duplicate them.
worker_class = "gthread"
threads = 4
keepalive = 2

# Print config on startup
print(f"[Gunicorn Config] CPU Cores: {cpu_cores}, Workers: {workers}, Threads/worker: {threads}, Bind: {bind}")
