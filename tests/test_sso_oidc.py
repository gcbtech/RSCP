import unittest
from unittest.mock import MagicMock, patch
import os
import sys

# Add app to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.services.db import init_db, get_db_connection
from app.services.auth import User

# Mock Authlib before it is imported by app
import sys
from unittest.mock import MagicMock
mock_authlib = MagicMock()
sys.modules['authlib'] = mock_authlib
sys.modules['authlib.integrations'] = mock_authlib
sys.modules['authlib.integrations.flask_client'] = mock_authlib

import sqlite3

class SharedConnection:
    """Wrapper to prevent closing the shared in-memory DB"""
    def __init__(self, conn):
        self.conn = conn
        self.row_factory = conn.row_factory
    
    def close(self):
        pass # Do nothing
        
    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)
        
    def commit(self):
        return self.conn.commit()
    
    def cursor(self):
        return self.conn.cursor()
        
    def __getattr__(self, name):
        return getattr(self.conn, name)

class TestSSO(unittest.TestCase):
    def setUp(self):
        # Use in-memory DB for tests
        self.app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'SECRET_KEY': 'test_secret'})
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()

        # Setup shared DB connection for :memory: persistence
        self.real_conn = sqlite3.connect(':memory:')
        self.real_conn.row_factory = sqlite3.Row
        self.db_conn = SharedConnection(self.real_conn)
        
        # Patch get_db_connection in all locations where it's imported
        # This is necessary because submodules import it at top-level
        self.patches = [
            patch('app.services.db.get_db_connection', return_value=self.db_conn),
            patch('app.services.auth.get_db_connection', return_value=self.db_conn),
            patch('app.routes.auth.get_db_connection', return_value=self.db_conn)
        ]
        
        for p in self.patches:
            p.start()
            
        # Initialize Schema
        init_db()

        # Mock OAuth attached to app
        self.mock_oauth = MagicMock()
        self.app.oauth = self.mock_oauth
        self.mock_remote = MagicMock()
        self.mock_oauth.rscp_sso = self.mock_remote
        self.app.oauth.rscp_sso = self.mock_remote

    def tearDown(self):
        for p in self.patches:
            p.stop()
        
        self.real_conn.close()
        
        self.app_context.pop()

    SSO_CONF = {'SSO_ENABLED': True, 'SSO_ALLOWED_DOMAINS': ['@company.com']}

    def _conf(self, **overrides):
        conf = dict(self.SSO_CONF)
        conf.update(overrides)
        return patch('app.services.data_manager.load_config', return_value=conf)

    def test_sso_login_redirect(self):
        """/login/sso initiates the OAuth flow when SSO is enabled and domain-restricted"""
        self.mock_remote.authorize_redirect.return_value = 'REDIRECT_OBJECT'
        with self._conf():
            self.client.get('/login/sso')
        self.mock_remote.authorize_redirect.assert_called()

    def test_sso_disabled_without_domain_allowlist(self):
        """SSO refuses to start when no allowed domain is configured"""
        with self._conf(SSO_ALLOWED_DOMAINS=[]):
            self.client.get('/login/sso')
        self.mock_remote.authorize_redirect.assert_not_called()

    def test_sso_disabled_when_switched_off(self):
        """SSO_ENABLED=False is honoured even if client credentials exist"""
        with self._conf(SSO_ENABLED=False):
            self.client.get('/login/sso')
        self.mock_remote.authorize_redirect.assert_not_called()

    def test_sso_callback_creates_user(self):
        """Callback creates a new user when auto-provisioning is enabled"""
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {
            'email': 'newuser@company.com', 'email_verified': True, 'name': 'New User'
        }
        with self._conf(SSO_ALLOW_NEW_USERS=True):
            with patch('app.routes.auth.login_user'):
                res = self.client.get('/login/callback')
        user = User.get_by_email('newuser@company.com')
        self.assertIsNotNone(user)
        self.assertEqual(user.username, 'newuser')
        self.assertEqual(user.auth_provider, 'oidc')
        self.assertEqual(res.status_code, 302)

    def test_sso_no_auto_provision_by_default(self):
        """Without SSO_ALLOW_NEW_USERS, an unknown email gets no account"""
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {'email': 'stranger@company.com', 'email_verified': True}
        with self._conf():
            with patch('app.routes.auth.login_user') as mock_login:
                self.client.get('/login/callback')
        self.assertIsNone(User.get_by_email('stranger@company.com'))
        mock_login.assert_not_called()

    def test_sso_does_not_link_by_username(self):
        """An SSO email must NOT take over the local account whose username
        matches its local part (bob@company.com -> local user 'bob')."""
        self.db_conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('bob', 'hash', 1)")
        self.db_conn.commit()
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {'email': 'bob@company.com', 'email_verified': True}
        with self._conf():
            with patch('app.routes.auth.login_user') as mock_login:
                self.client.get('/login/callback')
        bob = User.get_by_username('bob')
        self.assertIsNone(bob.email)
        mock_login.assert_not_called()

    def test_sso_logs_in_user_with_matching_email(self):
        """A user whose account email matches signs in"""
        self.db_conn.execute("INSERT INTO users (username, password_hash, is_admin, email) "
                             "VALUES ('bob', 'hash', 0, 'bob@company.com')")
        self.db_conn.commit()
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {'email': 'Bob@Company.com', 'email_verified': True}
        with self._conf():
            with patch('app.routes.auth.login_user') as mock_login:
                self.client.get('/login/callback')
        mock_login.assert_called_once()
        self.assertEqual(mock_login.call_args[0][0].username, 'bob')

    def test_sso_rejects_unverified_email(self):
        self.db_conn.execute("INSERT INTO users (username, password_hash, is_admin, email) "
                             "VALUES ('bob', 'hash', 0, 'bob@company.com')")
        self.db_conn.commit()
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {'email': 'bob@company.com', 'email_verified': False}
        with self._conf():
            with patch('app.routes.auth.login_user') as mock_login:
                self.client.get('/login/callback')
        mock_login.assert_not_called()

    def test_sso_domain_restriction(self):
        """Login is blocked if the domain is not in the allowlist"""
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {'email': 'hacker@evil.com', 'email_verified': True}
        with self._conf(SSO_ALLOW_NEW_USERS=True):
            res = self.client.get('/login/callback', follow_redirects=True)
        self.assertIn(b'domain is not authorized', res.data)
        self.assertIsNone(User.get_by_email('hacker@evil.com'))


if __name__ == '__main__':
    unittest.main()
