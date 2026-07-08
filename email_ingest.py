import imaplib
import email
from email.header import decode_header
import re
import datetime
import time
import logging

# Configure logging
logger = logging.getLogger(__name__)

# Try to import BeautifulSoup (bs4), handle if missing
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

def get_html_body(msg):
    """Extract HTML body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode(errors='ignore')
    else:
        if msg.get_content_type() == "text/html":
            return msg.get_payload(decode=True).decode(errors='ignore')
    return ""

def extract_order_id(text, subject):
    """
    Extract Order ID from text or subject.
    Amazon: 111-1234567-1234567
    eBay: 12-12345-12345 (User example: 26-14075-32104)
    """
    # Amazon Pattern (3-7-7 digits)
    amzn_match = re.search(r'\b\d{3}-\d{7}-\d{7}\b', text) or re.search(r'\b\d{3}-\d{7}-\d{7}\b', subject)
    if amzn_match:
        return amzn_match.group(0), 'Amazon'

    # eBay Pattern (2-5-5 digits based on manifest example 26-14075-32104)
    # Also generic support for "Order #12345" if labeled explicitly
    ebay_match = re.search(r'\b\d{2}-\d{5}-\d{5}\b', text) or re.search(r'\b\d{2}-\d{5}-\d{5}\b', subject)
    if ebay_match:
        return ebay_match.group(0), 'eBay'
        
    return None, None


def extract_qty(text):
    """Attempt to find quantity in text like 'Qty: 2' or 'Quantity: 3'."""
    # Look for explicit label
    match = re.search(r'(?:Qty|Quantity)\s*[:.\-]\s*(\d+)', text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 1

def parse_amazon_items(soup):
    """
    Parse Amazon HTML for items.
    Looks for table structures or repeated blocks with images and titles.
    Returns: List of {'name': str, 'image_url': str, 'quantity': int}
    """
    items = []
    
    # Strategy: Look for images that link to products, usually contained in a table cell or div
    # Amazon emails vary, but product images are often wrapped in 'a' tags linking to /dp/ or /gp/product/
    
    # Find all links to products
    product_links = soup.find_all('a', href=re.compile(r'/(dp|gp/product)/[A-Z0-9]{10}'))
    
    # Deduplicate by ASIN to avoid grabbing the same item twice (image link + text link)
    seen_asins = set()
    
    for link in product_links:
        href = link.get('href', '')
        asin_match = re.search(r'/(dp|gp/product)/([A-Z0-9]{10})', href)
        if not asin_match: continue
        
        asin = asin_match.group(2)
        if asin in seen_asins: continue
        seen_asins.add(asin)
        
        container = link.find_parent(['td', 'div'])
        if not container: continue
        
        # Try to find the image
        img_tag = link.find('img')
        if not img_tag:
            row = container.find_parent(['tr', 'div'])
            if row:
                img_tag = row.find('img', src=re.compile(r'images-amazon\.com|ssl-images-amazon\.com'))
        
        image_url = img_tag.get('src') if img_tag else ""
        if not image_url:
            image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"

        title = link.get_text(separator=' ', strip=True)
        if not title or len(title) < 5:
            if img_tag and img_tag.get('alt'):
                title = img_tag.get('alt')
        
        title = re.sub(r'Write a product review', '', title, flags=re.IGNORECASE).strip()
        
        # Extract Qty
        qty = 1
        # Look in the container text
        container_text = container.get_text(separator=' ')
        q = extract_qty(container_text)
        if q > 1: qty = q
        else:
             # Try parent/row text
             row = container.find_parent(['tr', 'div'])
             if row:
                 q = extract_qty(row.get_text(separator=' '))
                 if q > 1: qty = q

        if title:
            items.append({
                'name': title,
                'image_url': image_url,
                'quantity': qty,
                'asin': asin
            })
            
    return items

    return items

def parse_using_qty_heuristic(soup):
    """
    Fallback: Find items by locating 'Qty: X' text and looking at the container.
    """
    items = []
    # Find all text nodes matching Qty pattern
    # We use a regex compile to find the element containing the text
    qty_pattern = re.compile(r'(?:Qty|Quantity)\s*[:.\-]\s*(\d+)', re.IGNORECASE)
    
    # Find all elements that contain this text directly? 
    # soup.find_all(string=...) returns NavigableStrings.
    qty_nodes = soup.find_all(string=qty_pattern)
    
    # DEBUG:
    if not qty_nodes:
         # Try finding without pattern to see what text exists
         # print(f"DEBUG: No Qty nodes found. All strings: {[str(s) for s in soup.strings]}")
         pass
    
    for node in qty_nodes:
        qty_match = qty_pattern.search(node)
        if not qty_match: continue
        qty = int(qty_match.group(1))
        
        # Walk up to find a container that likely holds the whole item (tr or distinct div)
        # Usually it's a <td> or <div> inside a <tr>
        container = node.find_parent(['tr', 'tbody', 'div', 'td']) # Added 'td' as valid container termination
        if not container: continue
        
        # In this container, find Image and Title
        
        # Image Search
        # Broader regex to catch m.media-amazon.com, images-na.ssl..., etc.
        img_regex = re.compile(r'amazon|ebayimg', re.IGNORECASE)
        
        img = container.find('img', src=img_regex)
        if not img:
            # Look slightly wider if the container was too narrow (e.g. just the qty cell)
            parent = container.find_parent(['tr', 'table'])
            if parent:
                img = parent.find('img', src=img_regex)
                
        image_url = img.get('src', '') if img else ''
        
        # Title Search
        # Look for a link first
        title = ""
        link = container.find('a')
        if not link and parent:
             link = parent.find('a')
             
        if link:
            title = link.get_text(separator=' ', strip=True)
            
        if not title:
            # Try bold text?
            bold = container.find(['strong', 'b'])
            if not bold and parent:
                bold = parent.find(['strong', 'b'])
            if bold: title = bold.get_text(strip=True)
            
        # Cleanup Title
        title = re.sub(r'Qty\s*[:.\-]\s*\d+', '', title, flags=re.IGNORECASE).strip()
        title = re.sub(r'Write a product review', '', title, flags=re.IGNORECASE).strip()
        
        if title and len(title) > 3:
            # Deduplicate
            if any(i['name'] == title for i in items): continue
            
            items.append({
                'name': title,
                'image_url': image_url,
                'quantity': qty
            })
            
    return items

def parse_ebay_items(soup):
    """
    Parse eBay HTML for items.
    eBay structure: Often <table> with <img> in one col and Title in another.
    """
    items = []
    images = soup.find_all('img', src=re.compile(r'ebayimg\.com'))
    
    for img in images:
        src = img.get('src', '')
        if 'icon' in src or 'logo' in src or 'spacer' in src: continue
        
        title = img.get('alt', '')
        qty = 1
        
        parent = img.find_parent(['td', 'div'])
        if parent:
            # Look for title in parent/row
            row = parent.find_parent(['tr', 'table'])
            if row:
                row_text = row.get_text(separator=' ')
                q = extract_qty(row_text)
                if q > 1: qty = q
                
                if not title:
                    text_link = row.find('a')
                    if text_link:
                        title = text_link.get_text(strip=True)
        
        if title:
            if any(i['name'] == title for i in items): continue
            
            items.append({
                'name': title,
                'image_url': src,
                'quantity': qty
            })
            
    return items

def check_amazon_emails(imap_server, user, password):
    """
    Connects to IMAP, searches for unread emails, extracts Order IDs and Items.
    Generalized for Amazon AND eBay.
    """
    if not BS4_AVAILABLE:
        logger.error("Missing dependency: beautifulsoup4")
        return []

    results = []
    mail = None

    try:
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(user, password)
        mail.select("inbox")

        # Search UNREAD
        status, messages = mail.search(None, 'UNSEEN')
        if status != "OK": return []
        
        email_ids = messages[0].split()
        
        for email_id in email_ids[-20:]: # Process last 20 unread
            try:
                # Fetch full content
                res, msg_data = mail.fetch(email_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                
                # Subject
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding if encoding else "utf-8")
                
                # Body
                body_html = get_html_body(msg)
                if not body_html: 
                    logger.debug(f"Email {email_id} skipped: No HTML body.")
                    continue
                
                soup = BeautifulSoup(body_html, 'html.parser')
                text_content = soup.get_text(separator=' ')
                
                # 1. Identify Order ID
                order_id, source = extract_order_id(text_content, subject)
                
                logger.info(f"Processing Email {email_id} | Subject: {subject[:50]}... | Order ID: {order_id} | Source: {source}")
                
                if order_id:
                    tracking_number = f"ORDER-{order_id}"
                    
                    # 2. Extract Items
                    items = []
                    if source == 'Amazon':
                        items = parse_amazon_items(soup)
                    elif source == 'eBay':
                        items = parse_ebay_items(soup)
                        
                    # FALLBACK: If standard parsers failed to find items, try the Qty Heuristic
                    # This works for both Amazon and eBay if they follow the "Qty: X" pattern
                    if not items:
                        logger.info(f"Standard parsing returned 0 items for {tracking_number}. Trying Qty heuristic...")
                        items = parse_using_qty_heuristic(soup)

                    # 3. Create Records
                    if items:
                        # Deduplicate items list just in case
                        # (Heuristic might find same item twice)
                        unique_items = []
                        seen_names = set()
                        for i in items:
                            key = f"{i['name']}-{i['image_url']}"
                            if key in seen_names: continue
                            seen_names.add(key)
                            unique_items.append(i)

                        for idx, item in enumerate(unique_items, 1):
                            # Append suffix to ensure uniqueness in DB
                            # Format: ORDER-123-12345-01
                            suffix = f"-{idx:02d}"
                            unique_tracking = f"{tracking_number}{suffix}"
                            
                            results.append({
                                'tracking': unique_tracking,
                                'name': item['name'],
                                'image_url': item['image_url'],
                                'quantity': item['quantity'],
                                'date': datetime.datetime.now().strftime('%Y-%m-%d'),
                                'source': 'Auto-Email',
                                'status': 'incoming'
                            })
                    else:
                        # Final Fallback
                        logger.warning(f"Failed to parse items for {tracking_number}. Using Subject line.")
                        results.append({
                            'tracking': tracking_number,
                            'name': subject, # User warned this is bad, but better than nothing?
                            'image_url': '',
                            'quantity': 1,
                            'date': datetime.datetime.now().strftime('%Y-%m-%d'),
                            'source': 'Auto-Email',
                            'status': 'incoming'
                        })
                    
            except Exception as e:
                logger.error(f"Error processing email {email_id}: {e}")
                continue
                
        mail.close()
        mail.logout()
        
    except Exception as e:
        logger.error(f"IMAP Error: {e}")
        return []

    return results

