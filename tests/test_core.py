"""
RSCP Core Functionality Test Suite
Run with: python -m pytest tests/test_core.py -v

Tests core application functionality including:
- Authentication
- Package management
- Dashboard stats
- Configuration loading/saving
"""
import pytest
import tempfile
import json
import os
import sys
from datetime import date, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture
def app():
    """Create application for testing."""
    from app import create_app
    
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    
    yield app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


@pytest.fixture
def authenticated_client(client):
    """Create authenticated client for testing."""
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'  # Flask-Login session key
    return client


@pytest.fixture
def admin_client(client):
    """Create authenticated admin client for testing."""
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'  # Assume user 1 is admin
        sess['is_admin'] = True
    return client


class TestApplicationStartup:
    """Test application startup and configuration."""
    
    def test_app_creates_successfully(self, app):
        """Application should create without errors."""
        assert app is not None
        
    def test_app_has_secret_key(self, app):
        """Application should have a secret key configured."""
        assert app.secret_key is not None
        assert len(app.secret_key) > 0
    
    def test_rate_limiter_attached(self, app):
        """Rate limiter should be attached to app."""
        assert hasattr(app, 'limiter')


class TestConfigurationManagement:
    """Test configuration loading and saving."""
    
    def test_load_config_returns_dict(self):
        """load_config should return a dictionary."""
        from app.services.data_manager import load_config
        config = load_config()
        assert isinstance(config, dict)
    
    def test_load_config_caching(self):
        """Config should be cached to avoid repeated disk I/O."""
        from app.services.data_manager import load_config, CONFIG_CACHE
        
        # Clear cache
        CONFIG_CACHE['data'] = None
        
        # First call loads from disk
        config1 = load_config()
        load_time1 = CONFIG_CACHE['loaded_at']
        
        # Second call should use cache
        config2 = load_config()
        load_time2 = CONFIG_CACHE['loaded_at']
        
        # Times should be the same (cache hit)
        assert load_time1 == load_time2
    
    def test_force_reload_bypasses_cache(self):
        """force_reload=True should bypass the cache."""
        from app.services.data_manager import load_config, CONFIG_CACHE
        import time
        
        config1 = load_config()
        time1 = CONFIG_CACHE['loaded_at']
        
        time.sleep(0.1)
        
        config2 = load_config(force_reload=True)
        time2 = CONFIG_CACHE['loaded_at']
        
        # Force reload should update the time
        assert time2 > time1


class TestDatabaseConnection:
    """Test database connection utilities."""
    
    def test_connection_returns_cursor(self):
        """get_db_connection should return a valid connection."""
        from app.services.db import get_db_connection
        
        conn = get_db_connection()
        assert conn is not None
        
        # Should be able to execute queries
        result = conn.execute("SELECT 1 as test").fetchone()
        assert result['test'] == 1
        
        conn.close()
    
    def test_connection_has_row_factory(self):
        """Connection should have Row factory for dict-like access."""
        from app.services.db import get_db_connection
        import sqlite3
        
        conn = get_db_connection()
        assert conn.row_factory == sqlite3.Row
        conn.close()


class TestDashboardStats:
    """Test dashboard statistics."""
    
    def test_dashboard_stats_returns_dict(self):
        """get_dashboard_stats should return expected structure."""
        from app.services.data_manager import get_dashboard_stats
        
        stats = get_dashboard_stats()
        
        assert isinstance(stats, dict)
        assert 'expected' in stats
        assert 'past_due' in stats
        assert 'returns' in stats
        assert 'refunded' in stats
    
    def test_expected_stats_structure(self):
        """Expected stats should have total, scanned, and status."""
        from app.services.data_manager import get_dashboard_stats
        
        stats = get_dashboard_stats()
        expected = stats['expected']
        
        assert 'total' in expected
        assert 'scanned' in expected
        assert 'status' in expected


class TestAnalyticsStats:
    """Test analytics data generation."""
    
    def test_analytics_returns_list(self):
        """get_analytics_stats should return a list."""
        from app.services.data_manager import get_analytics_stats
        
        stats = get_analytics_stats(7)
        
        assert isinstance(stats, list)
        assert len(stats) == 7  # 7 days of data
    
    def test_analytics_entry_structure(self):
        """Each analytics entry should have date and count."""
        from app.services.data_manager import get_analytics_stats
        
        stats = get_analytics_stats(7)
        
        for entry in stats:
            assert 'date' in entry
            assert 'count' in entry


class TestPasswordValidation:
    """Test password complexity validation."""
    
    def test_short_password_rejected(self):
        """Passwords shorter than 8 characters should be rejected."""
        from app.routes.admin import validate_password
        
        valid, msg = validate_password("Short1!")
        assert valid is False
        assert "8 characters" in msg
    
    def test_no_uppercase_rejected(self):
        """Passwords without uppercase should be rejected."""
        from app.routes.admin import validate_password
        
        valid, msg = validate_password("lowercase123!")
        assert valid is False
        assert "uppercase" in msg
    
    def test_no_number_or_symbol_rejected(self):
        """Passwords without numbers/symbols should be rejected."""
        from app.routes.admin import validate_password
        
        valid, msg = validate_password("Uppercase")
        assert valid is False
        assert "number or symbol" in msg
    
    def test_valid_password_accepted(self):
        """Valid passwords should be accepted."""
        from app.routes.admin import validate_password
        
        valid, msg = validate_password("SecurePass123!")
        assert valid is True
        assert msg == ""


class TestRouteAccessibility:
    """Test that routes are accessible."""
    
    def test_login_page_accessible(self, client):
        """Login page should be accessible."""
        response = client.get('/login')
        assert response.status_code == 200
    
    def test_setup_page_accessible_when_no_users(self, client):
        """Setup page should redirect appropriately."""
        response = client.get('/setup')
        # Either accessible (no users) or redirects (users exist)
        assert response.status_code in [200, 302]
    
    def test_root_redirects(self, client):
        """Root should redirect to login or dashboard."""
        response = client.get('/')
        assert response.status_code in [200, 302]


class TestHelperFunctions:
    """Test utility helper functions."""
    
    def test_parse_date_valid_formats(self):
        """parse_date should handle various date formats."""
        from app.utils.helpers import parse_date
        
        # YYYY-MM-DD
        assert parse_date("2025-12-22") == "2025-12-22"
        
        # MM/DD/YYYY
        assert parse_date("12/22/2025") == "2025-12-22"
        
        # Pending
        assert parse_date("Pending") == "Pending"
        assert parse_date("pending") == "Pending"
        assert parse_date("") == "Pending"
    
    def test_sanitize_for_csv(self):
        """sanitize_for_csv should prevent CSV injection."""
        from app.utils.helpers import sanitize_for_csv
        
        # Normal text unchanged
        assert sanitize_for_csv("Normal text") == "Normal text"
        
        # Formula injection prevented
        assert sanitize_for_csv("=HYPERLINK()").startswith("'")
        assert sanitize_for_csv("+cmd").startswith("'")
        assert sanitize_for_csv("-cmd").startswith("'")
        assert sanitize_for_csv("@formula").startswith("'")
    
    def test_format_date_filter(self):
        """format_date_filter should format dates correctly."""
        from app.utils.helpers import format_date_filter
        
        # US format
        assert format_date_filter("2025-12-22", "US") == "12/22/2025"
        
        # EU format
        assert format_date_filter("2025-12-22", "EU") == "22/12/2025"
        
        # Pending unchanged
        assert format_date_filter("Pending") == "Pending"


class TestBackgroundTasks:
    """Test background task infrastructure."""
    
    def test_sync_status_structure(self):
        """get_sync_status should return expected structure."""
        from app.services.background_tasks import get_sync_status
        
        status = get_sync_status()
        
        assert isinstance(status, dict)
        assert 'last_manifest_sync' in status
        assert 'last_email_check' in status
        assert 'manifest_sync_count' in status
        assert 'errors' in status
class TestVersionComparison:
    """Test version comparison for update checks."""
    
    def test_newer_major_version(self):
        """Higher major version should be considered newer."""
        from app.routes.admin import version_is_newer
        
        assert version_is_newer("2.0.0", "1.16.5") is True
    
    def test_newer_minor_version(self):
        """Higher minor version should be considered newer."""
        from app.routes.admin import version_is_newer
        
        assert version_is_newer("1.17.0", "1.16.5") is True
    
    def test_newer_patch_version(self):
        """Higher patch version should be considered newer."""
        from app.routes.admin import version_is_newer
        
        assert version_is_newer("1.16.6", "1.16.5") is True
    
    def test_older_version_not_upgrade(self):
        """Older remote version should NOT trigger update."""
        from app.routes.admin import version_is_newer
        
        assert version_is_newer("1.16.2", "1.16.5") is False
    
    def test_same_version_not_upgrade(self):
        """Same version should NOT trigger update."""
        from app.routes.admin import version_is_newer
        
        assert version_is_newer("1.16.5", "1.16.5") is False
    
    def test_unknown_local_version(self):
        """Unknown local version should allow update."""
        from app.routes.admin import version_is_newer
        
        assert version_is_newer("1.16.5", "unknown") is True
    
    def test_invalid_version_no_update(self):
        """Invalid version strings should not trigger update."""
        from app.routes.admin import version_is_newer
        
        assert version_is_newer("invalid", "1.16.5") is False
        assert version_is_newer(None, "1.16.5") is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
