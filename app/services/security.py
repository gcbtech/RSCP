"""
Security Service
Handles application-level encryption for sensitive data (secrets, API keys).
Uses Fernet (symmetric encryption) with a key derived from the application SECRET_KEY.
"""
import base64
import hashlib
import logging
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

logger = logging.getLogger(__name__)

_cipher_suite = None
_last_secret_key = None

def get_cipher():
    """Get or create the Fernet cipher suite based on current app secret."""
    global _cipher_suite, _last_secret_key
    
    secret = current_app.config.get('SECRET_KEY', 'default-insecure-key')
    
    # Re-initialize if secret changed (unlikely but safe)
    if _cipher_suite is None or secret != _last_secret_key:
        # Derive a 32-byte url-safe base64 key from the secret
        # We use SHA256 digest of the secret
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        _cipher_suite = Fernet(key)
        _last_secret_key = secret
        
    return _cipher_suite

def encrypt(data):
    """
    Encrypt a string value.
    Returns: URL-safe base64 encoded bytes (as string).
    """
    if not data:
        return ""
    
    try:
        cipher = get_cipher()
        # Fernet expects bytes
        encrypted_bytes = cipher.encrypt(data.encode())
        return encrypted_bytes.decode()
    except Exception as e:
        logger.error(f"Encryption error: {e}")
        return data # Fallback to plaintext if encryption fails (should not happen)

def decrypt(token):
    """
    Decrypt a token.
    Graceful Fallback: If decryption fails (e.g. data is not encrypted), returns the original token.
    """
    if not token:
        return ""
        
    try:
        cipher = get_cipher()
        decrypted_bytes = cipher.decrypt(token.encode())
        return decrypted_bytes.decode()
    except InvalidToken:
        # This is expected for existing plaintext data
        return token
    except Exception as e:
        logger.warning(f"Decryption error (fallback to plaintext): {e}")
        return token # Safe fallback
