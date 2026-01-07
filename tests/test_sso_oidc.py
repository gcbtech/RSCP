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

    def test_sso_login_redirect(self):
        """Test that /login/sso initiates the OAuth flow"""
        self.mock_remote.authorize_redirect.return_value = 'REDIRECT_OBJECT'
        res = self.client.get('/login/sso')
        # We expect what authorize_redirect returns. In Flask client test, 
        # normally we check if it called the method.
        self.mock_remote.authorize_redirect.assert_called()

    def test_sso_callback_creates_user(self):
        """Test that callback creates a new user if config allows"""
        # Mock Token and UserInfo
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {
            'email': 'newuser@example.com', 
            'name': 'New User'
        }
        
        # Mock Config to allow new users
        with patch('app.services.data_manager.load_config') as mock_conf:
            mock_conf.return_value = {'SSO_ALLOW_NEW_USERS': True, 'SSO_ALLOWED_DOMAINS': []}
            
            with patch('app.routes.auth.login_user') as mock_login_user:
                res = self.client.get('/login/callback')
                
                # Check DB
                user = User.get_by_email('newuser@example.com')
                self.assertIsNotNone(user)
                self.assertEqual(user.username, 'newuser')
                self.assertEqual(user.auth_provider, 'oidc')
                self.assertEqual(res.status_code, 302) # Redirect to index

    def test_sso_callback_links_existing_user(self):
        """Test that callback links to an existing user by matching local-part of email"""
        # Create existing user "bob"
        self.db_conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('bob', 'hash', 0)")
        self.db_conn.commit()
        
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {'email': 'bob@company.com'}
        
        with patch('app.services.data_manager.load_config') as mock_conf:
            mock_conf.return_value = {'SSO_ALLOW_NEW_USERS': True}
            
            with patch('app.routes.auth.login_user'):
                res = self.client.get('/login/callback')
                
                # Verify bob is updated
                user = User.get_by_username('bob')
                self.assertIsNotNone(user)
                self.assertEqual(user.email, 'bob@company.com')
                self.assertEqual(user.auth_provider, 'oidc')

    def test_sso_domain_restriction(self):
        """Test that login is blocked if domain is not in whitelist"""
        self.mock_remote.authorize_access_token.return_value = {'access_token': 'fake'}
        self.mock_remote.userinfo.return_value = {'email': 'hacker@evil.com'}
        
        with patch('app.services.data_manager.load_config') as mock_conf:
            mock_conf.return_value = {'SSO_ALLOWED_DOMAINS': ['@good.com']}
            
            res = self.client.get('/login/callback', follow_redirects=True)
            
            # Should flash error and redirect to login
            self.assertIn(b'domain is not authorized', res.data)
            
            # Verify no user created
            user = User.get_by_email('hacker@evil.com')
            self.assertIsNone(user)

if __name__ == '__main__':
    unittest.main()
