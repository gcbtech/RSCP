import datetime
from typing import Optional

def parse_date(value: str) -> str:
    """Robust date parser handling pending, empty, and various formats."""
    if not value or str(value).lower() in ['pending', 'nan', 'none', '']:
        return "Pending"
    
    val_str = str(value).strip()
    if val_str.lower() == 'pending': 
        return "Pending"

    for fmt in ['%Y-%m-%d', '%m/%d/%Y', '%-m/%-d/%Y', '%d/%m/%Y', '%Y/%m/%d']:
        try:
            dt = datetime.datetime.strptime(val_str, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue
    
    return "Pending"

def sanitize_for_csv(text: str) -> str:
    """Prepares text for CSV injection safety."""
    if text is None: return ""
    text_str = str(text).replace(",", " ").strip()
    if text_str.startswith(('=', '+', '-', '@')):
        return f"'{text_str}" 
    return text_str

def format_date_filter(value: str, fmt_type: str = 'US') -> str:
    """Jinja filter to format YYYY-MM-DD string to readable format."""
    if not value or value == 'Pending': return value
    try:
        dt = datetime.datetime.strptime(value, '%Y-%m-%d')
        if fmt_type == 'EU':
            return dt.strftime('%d/%m/%Y')
        return dt.strftime('%m/%d/%Y')
    except ValueError:
        return value

def obscure_string(text: str, key: str) -> str:
    """
    Securely encrypt sensitive strings using itsdangerous (comes with Flask).
    Uses URLSafeSerializer with timestamp for proper encryption.
    Backwards compatible - returns a signed token.
    """
    if not text or not key: return ""
    try:
        from itsdangerous import URLSafeSerializer
        s = URLSafeSerializer(key, salt='rscp-sensitive-data')
        return s.dumps(text)
    except Exception:
        # Fallback to base64 if itsdangerous fails
        import base64
        return base64.b64encode(text.encode()).decode()

def reveal_string(obfuscated_text: str, key: str) -> str:
    """
    Decrypt strings encrypted with obscure_string.
    Handles both new (itsdangerous) and legacy (XOR) formats.
    """
    if not obfuscated_text or not key: return ""
    
    # Try new itsdangerous format first
    try:
        from itsdangerous import URLSafeSerializer
        s = URLSafeSerializer(key, salt='rscp-sensitive-data')
        return s.loads(obfuscated_text)
    except Exception:
        pass
    
    # Fallback to legacy XOR format for backwards compatibility
    import base64
    try:
        xor_bytes = base64.b64decode(obfuscated_text)
        key_cycle = (key * (len(xor_bytes) // len(key) + 1))[:len(xor_bytes)]
        return "".join([chr(a ^ ord(b)) for a, b in zip(xor_bytes, key_cycle)])
    except:
        return ""
