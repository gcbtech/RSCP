class RscpError(Exception):
    """Base class for known RSCP application errors."""
    def __init__(self, message, code="RSCP-999", status_code=500):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code

# Error Code Definitions
ERR_DB_CONNECT = "RSCP-100"
ERR_DB_QUERY = "RSCP-101"

ERR_AUTH_MISSING = "RSCP-200"
ERR_AUTH_INVALID = "RSCP-201"
ERR_PERM_DENIED = "RSCP-203"

ERR_INVENTORY_INVALID = "RSCP-300"
ERR_SKU_GENERATION = "RSCP-301"

ERR_SYSTEM_UNKNOWN = "RSCP-999"

# Helper for wrapping logic
def rscp_assert(condition, message, code=ERR_SYSTEM_UNKNOWN, status=500):
    if not condition:
        raise RscpError(message, code, status)
