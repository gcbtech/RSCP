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
    """Simple XOR obfuscation to prevent plain-text storage."""
    if not text or not key: return ""
    import base64
    
    # Pad key to match text length
    key_cycle = (key * (len(text) // len(key) + 1))[:len(text)]
    
    # XOR
    xor_bytes = bytes([ord(a) ^ ord(b) for a, b in zip(text, key_cycle)])
    
    # Base64 Encode
    return base64.b64encode(xor_bytes).decode('utf-8')

def reveal_string(obfuscated_text: str, key: str) -> str:
    """Reverses obscure_string."""
    if not obfuscated_text or not key: return ""
    import base64
    try:
        xor_bytes = base64.b64decode(obfuscated_text)
        key_cycle = (key * (len(xor_bytes) // len(key) + 1))[:len(xor_bytes)]
        return "".join([chr(a ^ ord(b)) for a, b in zip(xor_bytes, key_cycle)])
    except:
        return ""
