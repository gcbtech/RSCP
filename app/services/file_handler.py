import time
import os
import contextlib
from typing import Generator, TextIO, Union

class SimpleFileLock:
    """Simple cross-platform file locking using a .lock file."""
    def __init__(self, file_path: str, timeout: int = 5):
        self.lock_file = file_path + ".lock"
        self.timeout = timeout

    def __enter__(self):
        start_time = time.time()
        while os.path.exists(self.lock_file):
            # Check for stale lock (older than 30s)
            try:
                if time.time() - os.path.getmtime(self.lock_file) > 30:
                    os.remove(self.lock_file)
                    break
            except OSError: 
                pass
            
            if time.time() - start_time > self.timeout:
                raise TimeoutError(f"Could not acquire lock for {self.lock_file}")
            time.sleep(0.1)
        
        # Acquire lock
        try:
            with open(self.lock_file, 'w') as f:
                f.write(str(os.getpid()))
        except OSError:
            pass # Race condition potential, but acceptable for this scale

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if os.path.exists(self.lock_file):
                os.remove(self.lock_file)
        except OSError: 
            pass

@contextlib.contextmanager
def atomic_write(file_path: str, mode: str = 'w', encoding: str = 'utf-8') -> Generator[TextIO, None, None]:
    """Safe write: Write to .tmp then rename to target."""
    temp_path = file_path + ".tmp"
    try:
        with open(temp_path, mode, encoding=encoding) as f:
            yield f
        # Atomic rename (replace)
        os.replace(temp_path, file_path)
    except Exception as e:
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError: pass
        raise e
