"""
Gunicorn Configuration File
Usage: gunicorn -c gunicorn.conf.py wsgi:app
"""
import multiprocessing

# Calculate workers based on CPU cores
# Formula: (2 * cores) + 1, with a minimum of 2
cpu_cores = multiprocessing.cpu_count()
workers = min(max((2 * cpu_cores) + 1, 2), 8)  # Clamp between 2 and 8

# Binding
bind = "0.0.0.0:5000"

# Timeout for slow requests
timeout = 120

# Logging
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
# every terminal at once. Background tasks are started exactly once via a
# lock-file guard in wsgi.py, so scaling workers does not duplicate them.
worker_class = "gthread"
threads = 4
keepalive = 2

# Print config on startup
print(f"[Gunicorn Config] CPU Cores: {cpu_cores}, Workers: {workers}, Threads/worker: {threads}")
