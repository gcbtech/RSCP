"""
One-time script to optimize all existing inventory images.
Resizes to max 1280x720 and compresses to 85% JPEG quality.

RUN THIS DIRECTLY ON THE SERVER from the Dropzone directory:
  cd C:\path\to\Dropzone
  python scripts/optimize_images.py
"""
import os
import io
from PIL import Image

# Configuration
MAX_SIZE = (1280, 720)
JPEG_QUALITY = 85

# Use relative path from Dropzone root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)  # Go up from scripts/ to Dropzone/
UPLOADS_DIR = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')

def optimize_image(filepath):
    """Optimize a single image file."""
    try:
        with Image.open(filepath) as img:
            # Skip if already small enough
            if img.size[0] <= MAX_SIZE[0] and img.size[1] <= MAX_SIZE[1]:
                return False, "Already optimized"
            
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            
            # Resize
            img.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
            
            # Save back (as JPEG)
            new_path = os.path.splitext(filepath)[0] + '.jpg'
            img.save(new_path, format='JPEG', quality=JPEG_QUALITY, optimize=True)
            
            # Remove old file if different extension
            if new_path != filepath and os.path.exists(new_path):
                os.remove(filepath)
            
            return True, "Optimized"
    except Exception as e:
        return False, str(e)

def main():
    if not os.path.exists(UPLOADS_DIR):
        print(f"Directory not found: {UPLOADS_DIR}")
        return
    
    files = [f for f in os.listdir(UPLOADS_DIR) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))]
    print(f"Found {len(files)} images to process...")
    
    optimized = 0
    skipped = 0
    errors = 0
    
    for i, filename in enumerate(files, 1):
        filepath = os.path.join(UPLOADS_DIR, filename)
        success, msg = optimize_image(filepath)
        
        if success:
            optimized += 1
            print(f"[{i}/{len(files)}] OK {filename}")
        elif msg == "Already optimized":
            skipped += 1
            print(f"[{i}/{len(files)}] -- {filename} (already small)")
        else:
            errors += 1
            print(f"[{i}/{len(files)}] ERR {filename}: {msg}")
    
    print(f"\nDone! Optimized: {optimized}, Skipped: {skipped}, Errors: {errors}")

if __name__ == "__main__":
    main()
