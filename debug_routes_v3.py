
import sys
import os
from unittest.mock import MagicMock

# 1. Mock logging.handlers in sys.modules BEFORE importing anything else
mock_handlers = MagicMock()
sys.modules['logging.handlers'] = mock_handlers

import logging
# Ensure any direct access via logging.handlers works too
logging.handlers = mock_handlers

# Mock RotatingFileHandler specifically in the mock module
mock_handlers.RotatingFileHandler = MagicMock()

# Add CWD to path
sys.path.append(os.getcwd())

print("Attempting to create app with mocked logging...")

try:
    from app import create_app
    app = create_app()
    print("App created successfully.")
    
    print("\n--- TIMECLOCK ROUTES ---")
    found = False
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith('/timeclock'):
            print(f"{rule.endpoint}: {rule.rule}")
            found = True
            
    if not found:
        print("NO /timeclock ROUTES FOUND.")
        
except Exception as e:
    print(f"FAILED: {e}")
    import traceback
    traceback.print_exc()
