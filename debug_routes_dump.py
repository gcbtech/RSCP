
import sys
import os
from unittest.mock import MagicMock

# Validation: ensure mocking works
mock_handlers = MagicMock()
sys.modules['logging.handlers'] = mock_handlers
import logging
logging.handlers = mock_handlers
mock_handlers.RotatingFileHandler = MagicMock()

sys.path.append(os.getcwd())

try:
    from app import create_app
    app = create_app()
    
    with open('routes_dump.txt', 'w') as f:
        f.write("--- TIMECLOCK ROUTES ---\n")
        found = False
        for rule in sorted(list(app.url_map.iter_rules()), key=lambda r: r.rule):
            if rule.rule.startswith('/timeclock'):
                f.write(f"{rule.endpoint}: {rule.rule}\n")
                found = True
        
        if not found:
            f.write("NO /timeclock ROUTES FOUND.\n")
            
    print("Routes dumped to routes_dump.txt")
        
except Exception as e:
    print(f"FAILED: {e}")
