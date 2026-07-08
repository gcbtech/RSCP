"""
Fix script to update database image URLs after optimization.
Updates .png/.webp references to .jpg when the jpg file exists.

RUN THIS DIRECTLY ON THE SERVER from the Dropzone directory:
  cd C:\path\to\Dropzone
  python scripts/fix_image_paths.py
"""
import os
import sqlite3

# Use relative path from Dropzone root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(BASE_DIR, 'rscp.db')
UPLOADS_DIR = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')

def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        return
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all items with image URLs
    cursor.execute("SELECT id, image_url FROM inventory_items WHERE image_url IS NOT NULL AND image_url != ''")
    items = cursor.fetchall()
    
    print(f"Checking {len(items)} items with images...")
    
    fixed = 0
    already_ok = 0
    not_found = 0
    
    for item in items:
        image_url = item['image_url']
        
        # Skip external URLs
        if image_url.startswith('http'):
            already_ok += 1
            continue
        
        # Extract filename from URL
        if '/static/uploads/inventory/' in image_url:
            filename = image_url.split('/static/uploads/inventory/')[-1]
            filepath = os.path.join(UPLOADS_DIR, filename)
            
            # Check if file exists
            if os.path.exists(filepath):
                already_ok += 1
                continue
            
            # Try .jpg version
            base, ext = os.path.splitext(filename)
            if ext.lower() in ['.png', '.webp', '.gif']:
                jpg_filename = base + '.jpg'
                jpg_filepath = os.path.join(UPLOADS_DIR, jpg_filename)
                
                if os.path.exists(jpg_filepath):
                    # Update database
                    new_url = f'/static/uploads/inventory/{jpg_filename}'
                    cursor.execute("UPDATE inventory_items SET image_url = ? WHERE id = ?", 
                                   (new_url, item['id']))
                    fixed += 1
                    print(f"Fixed: {filename} -> {jpg_filename}")
                else:
                    not_found += 1
                    print(f"Missing: {filename} (no .jpg found)")
            else:
                not_found += 1
                print(f"Missing: {filename}")
        else:
            already_ok += 1
    
    conn.commit()
    conn.close()
    
    print(f"\nDone! Fixed: {fixed}, Already OK: {already_ok}, Not Found: {not_found}")

if __name__ == "__main__":
    main()
