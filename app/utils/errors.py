"""
RSCP Error Handling Module

Defines error codes and exception classes for consistent error handling.
See docs/ERROR_CODES.md for full documentation.
"""

class RscpError(Exception):
    """Base class for known RSCP application errors."""
    def __init__(self, message, code="RSCP-999", status_code=500):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# =============================================================================
# Error Code Definitions
# =============================================================================

# 1xx - Database Errors
ERR_DB_CONNECT = "RSCP-100"
ERR_DB_QUERY = "RSCP-101"
ERR_DB_MIGRATION = "RSCP-102"

# 2xx - Authentication & Authorization Errors
ERR_AUTH_MISSING = "RSCP-200"
ERR_AUTH_INVALID = "RSCP-201"
ERR_AUTH_EXPIRED = "RSCP-202"
ERR_PERM_DENIED = "RSCP-203"

# 3xx - Inventory Errors
ERR_INVENTORY_INVALID = "RSCP-300"
ERR_SKU_GENERATION = "RSCP-301"
ERR_INVENTORY_NOT_FOUND = "RSCP-302"
ERR_STOCK_INSUFFICIENT = "RSCP-303"

# 4xx - Package/Receiving Errors
ERR_PACKAGE_NOT_FOUND = "RSCP-400"
ERR_PACKAGE_DUPLICATE = "RSCP-401"
ERR_MANIFEST_PARSE = "RSCP-402"

# 5xx - POS Errors
ERR_POS_TRANSACTION = "RSCP-500"
ERR_POS_REFUND = "RSCP-501"
ERR_POS_INSUFFICIENT_STOCK = "RSCP-502"

# 6xx - Federation Errors
ERR_FED_PEER_OFFLINE = "RSCP-600"
ERR_FED_API_KEY = "RSCP-601"
ERR_FED_TRANSFER_EXPIRED = "RSCP-602"

# 9xx - System Errors
ERR_CONFIG_LOAD = "RSCP-900"
ERR_FILE_IO = "RSCP-901"
ERR_SYSTEM_UNKNOWN = "RSCP-999"


# =============================================================================
# Helper Functions
# =============================================================================

def rscp_assert(condition, message, code=ERR_SYSTEM_UNKNOWN, status=500):
    """Assert a condition, raising RscpError if false."""
    if not condition:
        raise RscpError(message, code, status)


def raise_db_error(message, original_exception=None):
    """Raise a database error with proper logging context."""
    raise RscpError(message, ERR_DB_QUERY, 500)


def raise_not_found(item_type, identifier):
    """Raise a not-found error with consistent messaging."""
    raise RscpError(f"{item_type} '{identifier}' not found", ERR_INVENTORY_NOT_FOUND, 404)
