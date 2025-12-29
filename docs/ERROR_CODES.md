# RSCP Error Code Reference

This document defines all RSCP error codes used by the application.

## Error Code Format

All error codes follow the pattern: `RSCP-XXX`

## Error Code Categories

### 1xx - Database Errors
| Code | Constant | Description |
|------|----------|-------------|
| RSCP-100 | ERR_DB_CONNECT | Failed to connect to database |
| RSCP-101 | ERR_DB_QUERY | Database query failed |
| RSCP-102 | ERR_DB_MIGRATION | Database migration failed |

### 2xx - Authentication & Authorization Errors
| Code | Constant | Description |
|------|----------|-------------|
| RSCP-200 | ERR_AUTH_MISSING | Authentication required but not provided |
| RSCP-201 | ERR_AUTH_INVALID | Invalid credentials (wrong password/PIN) |
| RSCP-202 | ERR_AUTH_EXPIRED | Session expired |
| RSCP-203 | ERR_PERM_DENIED | User lacks permission for this action |

### 3xx - Inventory Errors
| Code | Constant | Description |
|------|----------|-------------|
| RSCP-300 | ERR_INVENTORY_INVALID | Invalid inventory data |
| RSCP-301 | ERR_SKU_GENERATION | Failed to generate unique SKU |
| RSCP-302 | ERR_INVENTORY_NOT_FOUND | Inventory item not found |
| RSCP-303 | ERR_STOCK_INSUFFICIENT | Insufficient stock for operation |

### 4xx - Package/Receiving Errors
| Code | Constant | Description |
|------|----------|-------------|
| RSCP-400 | ERR_PACKAGE_NOT_FOUND | Package not found by tracking number |
| RSCP-401 | ERR_PACKAGE_DUPLICATE | Duplicate tracking number |
| RSCP-402 | ERR_MANIFEST_PARSE | Failed to parse manifest CSV |

### 5xx - POS Errors
| Code | Constant | Description |
|------|----------|-------------|
| RSCP-500 | ERR_POS_TRANSACTION | Transaction processing failed |
| RSCP-501 | ERR_POS_REFUND | Refund processing failed |
| RSCP-502 | ERR_POS_INSUFFICIENT_STOCK | Not enough stock to complete sale |

### 6xx - Federation Errors
| Code | Constant | Description |
|------|----------|-------------|
| RSCP-600 | ERR_FED_PEER_OFFLINE | Federation peer is unreachable |
| RSCP-601 | ERR_FED_API_KEY | Invalid federation API key |
| RSCP-602 | ERR_FED_TRANSFER_EXPIRED | Transfer request has expired |

### 9xx - System Errors
| Code | Constant | Description |
|------|----------|-------------|
| RSCP-900 | ERR_CONFIG_LOAD | Failed to load configuration |
| RSCP-901 | ERR_FILE_IO | File read/write error |
| RSCP-999 | ERR_SYSTEM_UNKNOWN | Unknown/unexpected system error (catch-all) |

## Usage

```python
from app.utils.errors import RscpError, ERR_INVENTORY_NOT_FOUND

# Raise a specific error
raise RscpError("Item with SKU ABC not found", ERR_INVENTORY_NOT_FOUND, 404)

# Use the assert helper
from app.utils.errors import rscp_assert, ERR_STOCK_INSUFFICIENT

rscp_assert(
    item.quantity >= requested,
    "Not enough stock",
    ERR_STOCK_INSUFFICIENT,
    400
)
```

## Error Page Display

When an error occurs, users see:
- **Error Code** - e.g., "RSCP-302"
- **Message** - Human-readable description
- **Request ID** - Unique identifier for support troubleshooting
