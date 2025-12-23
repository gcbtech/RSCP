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
worker_class = "sync"  # Default sync workers
keepalive = 2

# Print config on startup
print(f"[Gunicorn Config] CPU Cores: {cpu_cores}, Workers: {workers}")
