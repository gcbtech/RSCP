import imaplib
import email
from email.header import decode_header
import re
import datetime
import time

# Try to import BeautifulSoup (bs4), handle if missing
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

def get_text_from_html(html_content):
    if not BS4_AVAILABLE:
        return "BS4_MISSING"
    soup = BeautifulSoup(html_content, 'html.parser')
    # Kill script and style elements
    for script in soup(["script", "style"]):
        script.extract()
    return soup.get_text(separator=' ')

def extract_trackings(text):
    """
    Extract common tracking number formats.
    Targets:
    - 1Z... (UPS)
    - TBA... (Amazon Logistics)
    - 9... (USPS - simplified)
    """
    trackings = []
    
    # Regex Patterns
    # UPS: 1Z followed by 16 alphanum
    ups_pattern = r'\b1Z[A-Z0-9]{16}\b'
    
    # Amazon: TBA followed by 12 digits
    # Sometimes TBA is lowercase or mixed
    amzn_pattern = r'\bTBA[0-9]{12}\b'
    
    # USPS (Generic 22 digits starting with 9) - conservative approach
    usps_pattern = r'\b9[0-9]{21}\b'
    
    # FedEx (12 digits usually) - risky to match generic numbers, skipping for now to avoid noise
    
    # Find Matches
    for t in re.findall(ups_pattern, text, re.IGNORECASE): trackings.append(t.upper())
    for t in re.findall(amzn_pattern, text, re.IGNORECASE): trackings.append(t.upper())
    for t in re.findall(usps_pattern, text): trackings.append(t)
    
    # Deduplicate
    return list(set(trackings))

def extract_asin(text):
    """
    Extracts the first 10-char alphanumeric ASIN found in Amazon links.
    """
    # Pattern 1: /dp/B0...
    match = re.search(r'/dp/([A-Z0-9]{10})', text)
    if match: return match.group(1)
    
    # Pattern 2: /gp/product/B0...
    match = re.search(r'/gp/product/([A-Z0-9]{10})', text)
    if match: return match.group(1)
    
    return None

def check_amazon_emails(imap_server, user, password):
    """
    Connects to IMAP, searches for unread Amazon emails, extracts tracking numbers.
    Returns: List of dicts [{'tracking': '...', 'title': '...', 'date': '...'}, ...]
    """
    if not BS4_AVAILABLE:
        return {"error": "Missing dependency: beautifulsoup4. Please run 'pip install beautifulsoup4'."}

    results = []
    mail = None

    try:
        # 1. Connect
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(user, password)
        mail.select("inbox")

        # 2. Search Unread from Amazon
        # Note: "FROM" search can be strict. "auto-shipping@amazon.com" is common, but may vary.
        # We'll search UNREAD first to minimize load, then filter by sender in loop if needed.
        status, messages = mail.search(None, '(UNREAD)')
        
        if status != "OK":
            return []

        email_ids = messages[0].split()
        
        # Process latest 20 emails max to prevent timeout
        for email_id in email_ids[-20:]:
            try:
                # Fetch
                res, msg_data = mail.fetch(email_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        
                        # Filter Sender (Basic check)
                        from_header = msg.get("From", "").lower()
                        if "amazon" not in from_header and "shipping" not in from_header:
                            continue # Skip non-shipping emails (conservative)

                        # Decode Subject
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding if encoding else "utf-8")
                        
                        # Get Body
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                content_disposition = str(part.get("Content-Disposition"))
                                if "attachment" not in content_disposition:
                                    if content_type == "text/html":
                                        body = part.get_payload(decode=True).decode()
                                        break # Prefer HTML
                                    elif content_type == "text/plain" and not body:
                                        body = part.get_payload(decode=True).decode()
                        else:
                            body = msg.get_payload(decode=True).decode()

                        # Parse Text
                        text_content = get_text_from_html(body)
                        found_trackings = extract_trackings(text_content)
                        
                        # Extract ASIN from original HTML body (links are in HTML)
                        # We use the raw body for link searching, not the stripped text
                        asin = extract_asin(body)
                        image_url = None
                        if asin:
                            image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                        
                        # Extract Items (Very heuristic - looking for text near "Arriving" or simple subject lines)
                        # For now, use Subject as fallback Title
                        item_title = subject or "Amazon Order"

                        # Create Result
                        for t in found_trackings:
                            results.append({
                                'tracking': t,
                                'name': item_title, # Mapping 'title' to 'name' for DB consistency
                                'date': datetime.datetime.now().strftime('%Y-%m-%d'),
                                'source': 'Auto-Email',
                                'image_url': image_url
                            })
                            
            except Exception as e:
                print(f"Error processing email {email_id}: {e}")
                continue

        # Logout
        mail.close()
        mail.logout()

    except Exception as e:
        print(f"IMAP Error: {e}")
        return []

    return results
