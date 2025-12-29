"""
Inventory Core Module
Helper functions, before_request hooks, and overview routes.
"""
import logging
from datetime import datetime, date, timedelta
from flask import request, redirect, url_for, flash, render_template, jsonify
from flask_login import login_required, current_user

from app.routes.inventory import inventory_bp, CATEGORY_CODES
from app.services.db import get_db_connection
from app.services.data_manager import load_config

logger = logging.getLogger(__name__)


def is_inventory_enabled():
    """Check if inventory module is enabled in config."""
    config = load_config()
    return config.get('INVENTORY_ENABLED', False) if config else False


def generate_sku(category='ATO'):
    """Generate a unique SKU in format RSCP-XXX-XXXX-XXXX."""
    category = category.upper()[:3].ljust(3, 'X')
    
    conn = get_db_connection()
    try:
        # Find the highest existing SKU number for this category
        result = conn.execute('''
            SELECT sku FROM inventory_items 
            WHERE sku LIKE ? 
            ORDER BY sku DESC LIMIT 1
        ''', (f'RSCP-{category}-%',)).fetchone()
        
        if result:
            try:
                parts = result['sku'].split('-')
                if len(parts) == 4:
                    num_str = parts[2] + parts[3]
                    next_num = int(num_str) + 1
                else:
                    next_num = 1
            except:
                next_num = 1
        else:
            next_num = 1
        
        # Try up to 10 times to find a unique SKU
        for attempt in range(10):
            num_str = str(next_num + attempt).zfill(8)
            sku = f"RSCP-{category}-{num_str[:4]}-{num_str[4:]}"
            
            # Verify this SKU doesn't already exist
            existing = conn.execute(
                'SELECT 1 FROM inventory_items WHERE sku = ?', (sku,)
            ).fetchone()
            
            if not existing:
                return sku
        
        # Fallback: use timestamp to ensure uniqueness
        import time
        timestamp = str(int(time.time()))[-8:]
        sku = f"RSCP-{category}-{timestamp[:4]}-{timestamp[4:]}"
        return sku
    finally:
        conn.close()


def validate_location(area, aisle, shelf, bin_loc):
    """Validate that at least one location field is provided."""
    return any([area, aisle, shelf, bin_loc])


def get_inventory_stats():
    """Helper to calculate inventory statistics."""
    conn = get_db_connection()
    try:
        stats = {}
        conf = load_config()
        low_threshold = int(conf.get('LOW_STOCK_THRESHOLD', 5))
        
        stats['total_items'] = conn.execute('SELECT COUNT(*) FROM inventory_items').fetchone()[0]
        stats['total_quantity'] = conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM inventory_items').fetchone()[0]
        stats['out_of_stock'] = conn.execute('SELECT COUNT(*) FROM inventory_items WHERE quantity <= 0').fetchone()[0]
        stats['low_stock'] = conn.execute('''
            SELECT COUNT(*) FROM inventory_items 
            WHERE quantity > 0 
            AND (
                (COALESCE(alert_threshold, 0) > 0 AND quantity <= alert_threshold)
                OR 
                (COALESCE(alert_threshold, 0) = 0 AND ? > 0 AND quantity <= ?)
            )
        ''', (low_threshold, low_threshold)).fetchone()[0]
        
        # Profit Calculation
        financials = conn.execute('''
            SELECT 
                SUM(quantity * sell_price) as revenue,
                SUM(quantity * buy_price) as cost
            FROM inventory_items
            WHERE quantity > 0 AND buy_price > 0 AND sell_price > 0
        ''').fetchone()
        
        revenue = financials['revenue'] or 0
        cost = financials['cost'] or 0
        potential_profit = revenue - cost
        margin_percent = 0
        if revenue > 0:
            margin_percent = round((potential_profit / revenue) * 100, 1)
        
        stats['margin_percent'] = margin_percent
        stats['potential_profit'] = potential_profit

        # Last Audit Date
        last_audit = conn.execute("SELECT end_time FROM audit_sessions WHERE status='completed' ORDER BY end_time DESC LIMIT 1").fetchone()
        last_audit_str = "Never"
        if last_audit and last_audit['end_time']:
            audit_dt = datetime.strptime(str(last_audit['end_time']).split('.')[0], '%Y-%m-%d %H:%M:%S')
            delta = datetime.now() - audit_dt
            
            if delta.days >= 1:
                last_audit_str = f"{delta.days} days"
            else:
                seconds = delta.seconds
                if seconds < 3600:
                    mins = int(seconds / 60)
                    last_audit_str = f"{mins} mins"
                else:
                    hours = int(seconds / 3600)
                    last_audit_str = f"{hours} hours"
        
        stats['last_audit'] = last_audit_str
        return stats
    finally:
        conn.close()


@inventory_bp.before_request
def check_inventory_enabled():
    """Redirect if inventory module is disabled or user lacks role."""
    from flask_login import current_user
    
    if request.endpoint and 'api' in request.endpoint:
        return
    
    if not is_inventory_enabled():
        flash("Inventory module is not enabled.")
        return redirect(url_for('main.index'))
    
    # Check user role (admins bypass role check)
    if current_user.is_authenticated and not current_user.is_admin:
        if not current_user.has_role('inventory'):
            flash("You don't have access to the Inventory module.")
            return redirect(url_for('main.index'))


@inventory_bp.route('/')
def overview():
    """Inventory analytics overview page."""
    stats = get_inventory_stats()
    
    conn = get_db_connection()
    try:
        thirty_days_ago = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
        top_movers = conn.execute('''
            SELECT i.id, i.name, i.sku, ABS(SUM(t.quantity_change)) as sold
            FROM inventory_transactions t
            JOIN inventory_items i ON t.inventory_item_id = i.id
            WHERE t.quantity_change < 0 
              AND t.reason IN ('Sold/Consumed', 'Damaged')
              AND t.created_at > ?
            GROUP BY i.id
            ORDER BY sold DESC
            LIMIT 5
        ''', (thirty_days_ago,)).fetchall()
        
        conf = load_config()
        low_threshold = int(conf.get('LOW_STOCK_THRESHOLD', 5))
        
        attention_items = conn.execute('''
            SELECT id, name, quantity 
            FROM inventory_items 
            WHERE quantity <= 0
            OR (
                quantity > 0 AND (
                    (COALESCE(alert_threshold, 0) > 0 AND quantity <= alert_threshold)
                    OR 
                    (COALESCE(alert_threshold, 0) = 0 AND ? > 0 AND quantity <= ?)
                )
            )
            ORDER BY quantity ASC, name ASC
            LIMIT 10
        ''', (low_threshold, low_threshold)).fetchall()
        
        # Sales trend
        sales_data = conn.execute('''
            SELECT date(created_at) as day, COALESCE(SUM(ABS(quantity_change)), 0) as cnt
            FROM inventory_transactions 
            WHERE quantity_change < 0 
              AND reason = 'Sold/Consumed'
              AND date(created_at) >= ?
            GROUP BY date(created_at)
        ''', (thirty_days_ago,)).fetchall()
        
        sales_dict = {row['day']: row['cnt'] for row in sales_data}
        
        sales_trend = []
        for i in range(30, -1, -1):
            day = (date.today() - timedelta(days=i)).strftime('%Y-%m-%d')
            count = sales_dict.get(day, 0)
            sales_trend.append({'date': day[-5:], 'count': count})
        
        return render_template('inventory/overview.html',
                               stats=stats,
                               top_movers=top_movers,
                               attention_items=attention_items,
                               sales_trend=sales_trend)
    finally:
        conn.close()


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


@inventory_bp.route('/fetch-image-from-url', methods=['POST'])
@login_required
def fetch_image_from_url():
    """API endpoint to fetch product image from eBay/Amazon URL."""
    import re
    import requests as http_requests
    
    data = request.get_json()
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    
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
                except:
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
