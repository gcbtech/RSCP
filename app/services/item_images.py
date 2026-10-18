"""
Inventory item images: optimize uploads, store them under the uploads
directory (app.paths), and keep the 40x40 list thumbnails in step.

Thumbnails are generated at upload time, never while rendering the items
list (doing PIL work per row stalled whole pages).

No Flask request objects in here: callers pass the uploaded file objects
(anything with .filename and .read()) or raw bytes.
"""
import io
import json
import logging
import os
import time
import uuid

from werkzeug.utils import secure_filename

from app.paths import upload_path, upload_url, upload_url_to_path

logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = (1280, 720)  # Max resolution (720p)
JPEG_QUALITY = 85
THUMB_SIZE = (40, 40)

_INVENTORY_URL_PREFIX = '/static/uploads/inventory/'


def optimize_image(image_data, max_size=MAX_IMAGE_SIZE, quality=JPEG_QUALITY):
    """Resize (to fit max_size) and re-encode as JPEG.

    image_data: bytes or a file-like object. Returns JPEG bytes; if PIL is
    missing or the image can't be processed, the original bytes come back.
    """
    try:
        from PIL import Image

        if hasattr(image_data, 'read'):
            img = Image.open(image_data)
        else:
            img = Image.open(io.BytesIO(image_data))

        # Convert to RGB if necessary (for PNG with transparency)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        output = io.BytesIO()
        img.save(output, format='JPEG', quality=quality, optimize=True)
        return output.getvalue()

    except ImportError:
        logger.warning("PIL not installed, skipping image optimization")
        if hasattr(image_data, 'read'):
            return image_data.read()
        return image_data
    except Exception as e:
        logger.error(f"Image optimization failed: {e}")
        if hasattr(image_data, 'read'):
            return image_data.read()
        return image_data


def get_thumbnail_url(image_url):
    """Cheap thumbnail lookup for list rendering: path check only, no PIL.

    Returns None when no thumbnail exists; the template falls back to the
    full image.
    """
    if not image_url or image_url.startswith('http'):
        return None
    if _INVENTORY_URL_PREFIX not in image_url:
        return None
    filename = image_url.split(_INVENTORY_URL_PREFIX)[-1]
    base, _ext = os.path.splitext(filename)
    thumb_rel = f"uploads/inventory/thumbs/{base}_thumb.jpg"
    if os.path.exists(upload_path(*thumb_rel.split('/')[1:])):
        return f"/static/{thumb_rel}"
    return None


def generate_thumbnail(image_url):
    """Generate (or reuse a fresh) 40x40 thumbnail for an uploaded image.

    image_url: URL path like /static/uploads/inventory/image.jpg.
    Returns the thumbnail URL path, or None if it could not be generated.
    """
    if not image_url or image_url.startswith('http'):
        return None  # Skip external URLs

    try:
        from PIL import Image

        if _INVENTORY_URL_PREFIX not in image_url:
            return None

        filename = image_url.split(_INVENTORY_URL_PREFIX)[-1]
        source_path = upload_path('inventory', filename)
        if not os.path.exists(source_path):
            return None

        thumb_dir = upload_path('inventory', 'thumbs')
        os.makedirs(thumb_dir, exist_ok=True)

        base, _ext = os.path.splitext(filename)
        thumb_filename = f"{base}_thumb.jpg"
        thumb_path = os.path.join(thumb_dir, thumb_filename)

        # Skip if thumbnail already exists and is newer than source
        if os.path.exists(thumb_path):
            if os.path.getmtime(thumb_path) >= os.path.getmtime(source_path):
                return f"/static/uploads/inventory/thumbs/{thumb_filename}"

        with Image.open(source_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            img.save(thumb_path, format='JPEG', quality=80, optimize=True)

        return f"/static/uploads/inventory/thumbs/{thumb_filename}"

    except ImportError:
        logger.warning("PIL not installed, cannot generate thumbnail")
        return None
    except Exception as e:
        logger.error(f"Thumbnail generation failed for {image_url}: {e}")
        return None


def _write_upload(data, filename):
    upload_folder = upload_path('inventory')
    os.makedirs(upload_folder, exist_ok=True)
    with open(os.path.join(upload_folder, filename), 'wb') as f:
        f.write(data)
    return upload_url(f'inventory/{filename}')


def save_cover_upload(file, sku):
    """Store an uploaded cover image for `sku`. Returns its URL, or None when
    no file was chosen."""
    if not file or not getattr(file, 'filename', None):
        return None
    unique_name = f"{secure_filename(sku)}_{int(time.time())}.jpg"
    url = _write_upload(optimize_image(file), unique_name)
    generate_thumbnail(url)
    return url


def save_gallery_uploads(files, sku):
    """Store uploaded gallery images for `sku`. Returns their URLs in order."""
    urls = []
    for i, file in enumerate(files or []):
        if file and getattr(file, 'filename', None):
            unique_name = f"{secure_filename(sku)}_gallery_{i}_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
            url = _write_upload(optimize_image(file), unique_name)
            generate_thumbnail(url)
            urls.append(url)
    return urls


def save_camera_photo(image_binary):
    """Store a photo taken with the in-page camera (raw bytes). Returns its URL."""
    filename = f"{uuid.uuid4().hex}.jpg"
    upload_dir = upload_path('inventory')
    os.makedirs(upload_dir, exist_ok=True)
    with open(os.path.join(upload_dir, filename), 'wb') as f:
        f.write(optimize_image(image_binary))
    photo_url = f'{_INVENTORY_URL_PREFIX}{filename}'
    generate_thumbnail(photo_url)
    return photo_url


def delete_image_file(img):
    """Delete an uploaded image (and its thumbnail) from disk. Only local
    /static/ URLs are touched; errors are logged, never raised."""
    if not img or not img.startswith('/static/'):
        return
    file_path = upload_url_to_path(img) or ''
    if not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
        thumb_url = generate_thumbnail(img)
        if thumb_url and thumb_url.startswith('/static/'):
            thumb_path = upload_url_to_path(thumb_url) or ''
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
    except Exception as e:
        logger.error(f"Error deleting image file: {e}")


def parse_gallery(additional_images_json):
    """additional_images column (JSON list or NULL) -> list."""
    if not additional_images_json:
        return []
    try:
        value = json.loads(additional_images_json)
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def apply_gallery_edits(cover, gallery, delete_images=(), form_image_url='', swap_cover=''):
    """Apply the edit form's image changes to (cover, gallery) and return the
    new pair. Deleted gallery images are removed from disk; a fetched image
    URL replaces the cover; swap_cover promotes a gallery image and moves
    the old cover into the gallery."""
    gallery = list(gallery)
    for img in delete_images:
        if img in gallery:
            gallery.remove(img)
            delete_image_file(img)

    if form_image_url:
        cover = form_image_url

    if swap_cover and swap_cover in gallery:
        old_cover = cover
        cover = swap_cover
        gallery.remove(swap_cover)
        if old_cover:
            gallery.append(old_cover)
    return cover, gallery
