"""
Inventory Core Module
Helper functions, before_request hooks, and overview routes.
"""
import logging
from flask import request, redirect, url_for, flash, render_template, jsonify
from flask_login import login_required, current_user

from app.routes.inventory import inventory_bp, CATEGORY_CODES
from app.services.db import get_db_connection, get_request_db
from app.services import inventory
from app.services.data_manager import load_config
from app.utils.permissions import require_permission

logger = logging.getLogger(__name__)


def is_inventory_enabled():
    """Check if inventory module is enabled in config."""
    config = load_config()
    return config.get('INVENTORY_ENABLED', False) if config else False


def generate_sku(category='ATO', conn=None):
    """Generate a unique SKU in format RSCP-XXX-XXXX-XXXX."""
    if conn is not None:
        return inventory.generate_sku(conn, category)
    conn = get_db_connection()
    try:
        return inventory.generate_sku(conn, category)
    finally:
        conn.close()


validate_location = inventory.validate_location


def get_inventory_item(sku, conn=None):
    """Get an inventory item (dict with sale pricing) by SKU, UPC, or Part Number."""
    close_conn = False
    if conn is None:
        try:
            from flask import has_app_context
            if has_app_context():
                conn = get_request_db()
            else:
                conn = get_db_connection()
                close_conn = True
        except ImportError:
            conn = get_db_connection()
            close_conn = True
    try:
        return inventory.find_item_by_code(conn, sku)
    except Exception as e:
        logger.error(f"Error looking up inventory item: {e}")
        return None
    finally:
        if close_conn:
            conn.close()


def _low_stock_threshold():
    conf = load_config()
    return int(conf.get('LOW_STOCK_THRESHOLD', 5))


def get_inventory_stats():
    """Helper to calculate inventory statistics."""
    conn = get_db_connection()
    try:
        return inventory.inventory_stats(conn, _low_stock_threshold())
    finally:
        conn.close()


@inventory_bp.route('/')
def overview():
    """Inventory analytics overview page."""
    low_threshold = _low_stock_threshold()
    conn = get_db_connection()
    try:
        stats = inventory.inventory_stats(conn, low_threshold)
        data = inventory.overview_data(conn, low_threshold)
        return render_template('inventory/overview.html', stats=stats, **data)
    finally:
        conn.close()


ATTENTION_SNOOZE_DAYS = inventory.ATTENTION_SNOOZE_DAYS


@inventory_bp.route('/attention/<int:item_id>/snooze', methods=['POST'])
@require_permission('inventory.manage')
def snooze_attention_item(item_id):
    """Hide an item from 'Items Needing Attention' for ATTENTION_SNOOZE_DAYS days."""
    conn = get_db_connection()
    try:
        name, until = inventory.snooze_attention(conn, item_id, ATTENTION_SNOOZE_DAYS)
        flash(f"Snoozed '{name}' until {until.strftime('%b %d')}.")
    except inventory.ItemNotFound:
        flash("Item not found.")
    finally:
        conn.close()
    return redirect(url_for('inventory.overview'))


@inventory_bp.route('/api/overview')
def overview_api():
    """API to get current stats for auto-refresh."""
    return jsonify(get_inventory_stats())


@inventory_bp.route('/generate-sku')
def generate_sku_api():
    """API endpoint to generate a new SKU for a given category."""
    category = request.args.get('category', 'ATO')
    try:
        new_sku = generate_sku(category)
        return jsonify({'sku': new_sku})
    except Exception as e:
        logger.error(f"Error generating SKU: {e}")
        return jsonify({'error': str(e)}), 500


@inventory_bp.route('/check-sku-exists')
def check_sku_exists():
    """API endpoint to check if a SKU already exists."""
    sku = request.args.get('sku', '').strip()
    
    if not sku:
        return jsonify({'error': 'No SKU provided'}), 400
    
    try:
        conn = get_db_connection()
        existing = conn.execute(
            'SELECT id FROM inventory_items WHERE sku = ?', (sku,)
        ).fetchone()
        conn.close()
        
        return jsonify({
            'exists': existing is not None,
            'sku': sku
        })
    except Exception as e:
        logger.error(f"Error checking SKU: {e}")
        return jsonify({'error': str(e)}), 500


def validate_url_safe(url):
    """
    Validate URL is safe from SSRF attacks.
    
    Returns: (is_safe, error_message)
    """
    from urllib.parse import urlparse
    import socket
    
    parsed = urlparse(url)
    
    # Must be HTTP/HTTPS
    if parsed.scheme not in ('http', 'https'):
        return False, "Only HTTP/HTTPS URLs allowed"
    
    hostname = parsed.hostname or ''
    
    # Block localhost variations
    if hostname in ('localhost', '127.0.0.1', '::1', '0.0.0.0', ''):
        return False, "Internal addresses not allowed"
    
    # Block private IP ranges by resolving hostname
    try:
        ip = socket.gethostbyname(hostname)
        # Private ranges: 10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x (link-local/metadata)
        private_prefixes = (
            '10.', '172.16.', '172.17.', '172.18.', '172.19.',
            '172.20.', '172.21.', '172.22.', '172.23.', '172.24.',
            '172.25.', '172.26.', '172.27.', '172.28.', '172.29.',
            '172.30.', '172.31.', '192.168.', '127.', '169.254.', '0.'
        )
        if ip.startswith(private_prefixes):
            return False, "Internal addresses not allowed"
    except socket.gaierror:
        pass  # DNS resolution failed, let requests handle it
    
    # Restrict to known e-commerce domains for product images
    allowed_domains = (
        'ebay.com', 'ebayimg.com', 'ebaystatic.com',
        'amazon.com', 'ssl-images-amazon.com', 'images-na.ssl-images-amazon.com',
        'walmart.com', 'walmartimages.com',
        'target.com', 'scene7.com',
        'aliexpress.com', 'alicdn.com'
    )
    if not any(hostname.endswith(d) or hostname == d for d in allowed_domains):
        return False, f"Domain not in allow-list. Allowed: eBay, Amazon, Walmart, Target, AliExpress"
    
    return True, None


@inventory_bp.route('/fetch-image-from-url', methods=['POST'])
@require_permission('inventory.manage')
def fetch_image_from_url():
    """API endpoint to fetch product image from eBay/Amazon URL."""
    import re
    import requests as http_requests
    
    data = request.get_json()
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    
    # SSRF Protection: Validate URL before fetching
    is_safe, error = validate_url_safe(url)
    if not is_safe:
        logger.warning(f"SSRF attempt blocked: {url} - {error}")
        return jsonify({'error': error}), 400
    
    try:
        # Fetch the page
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = http_requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html = response.text

        
        image_url = None
        
        # Try different patterns to find the main product image
        
        # 1. eBay: og:image meta tag (most reliable)
        og_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not og_match:
            og_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, re.IGNORECASE)
        
        if og_match:
            image_url = og_match.group(1)
        
        # 2. eBay specific: data-zoom-src or data-src in image tags
        if not image_url and 'ebay' in url.lower():
            ebay_match = re.search(r'data-zoom-src=["\']([^"\']+)["\']', html)
            if not ebay_match:
                ebay_match = re.search(r'<img[^>]+class=["\'][^"\']*(?:vi-image|zoom)[^"\']*["\'][^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if ebay_match:
                image_url = ebay_match.group(1)
        
        # 3. Amazon: Try to extract ASIN and build image URL
        if not image_url and 'amazon' in url.lower():
            asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
            if asin_match:
                asin = asin_match.group(1)
                image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX400_.jpg"
        
        # 4. Generic: Look for large images in img tags
        if not image_url:
            img_matches = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
            for img in img_matches:
                # Skip small icons, tracking pixels, etc.
                if any(x in img.lower() for x in ['1x1', 'pixel', 'tracking', 'logo', 'icon', 'sprite', '.gif']):
                    continue
                # Prefer larger images
                if any(x in img.lower() for x in ['s-l1600', 's-l1200', 's-l800', 'large', 'zoom', 'main']):
                    image_url = img
                    break
        
        if image_url:
            # Clean up the URL (handle relative URLs, escaped characters)
            image_url = image_url.replace('&amp;', '&')
            if image_url.startswith('//'):
                image_url = 'https:' + image_url
            
            # Validate that this looks like a real product image
            # Exclude common placeholder/logo patterns
            invalid_patterns = ['placeholder', 'default', 'noimage', 'no-image', 'blank', 
                                'logo', 'spacer', 'transparent', 'missing', '1x1', 'pixel']
            if any(p in image_url.lower() for p in invalid_patterns):
                return jsonify({'error': 'Only found placeholder/logo image, not a product image'}), 404
            
            # Verify it looks like an image URL
            image_extensions = ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp']
            looks_like_image = any(ext in image_url.lower() for ext in image_extensions) or 'image' in image_url.lower()
            
            if not looks_like_image:
                # Could be a dynamic image URL without extension - try a HEAD request to verify
                try:
                    head_response = http_requests.head(image_url, headers=headers, timeout=5, allow_redirects=True)
                    content_type = head_response.headers.get('Content-Type', '')
                    if 'image' not in content_type:
                        return jsonify({'error': 'URL does not appear to be an image'}), 404
                except Exception:
                    pass  # If HEAD fails, still try to use the URL
            
            return jsonify({'image_url': image_url})
        else:
            return jsonify({'error': 'Could not find product image on the page'}), 404
            
    except http_requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Try again.'}), 408
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return jsonify({'error': f'Could not fetch the page: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Error parsing page for image: {e}")
        return jsonify({'error': str(e)}), 500
