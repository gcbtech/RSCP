"""
Inventory CSV Import Module
Handles bulk import of inventory items from CSV files.
"""
import io
import csv
import logging
from flask import render_template, request, redirect, url_for, flash, Response, session
from flask_login import login_required, current_user
from app.utils.permissions import require_permission
from app.routes.inventory import inventory_bp
from app.services.db import get_db_connection
from app.services import inventory

logger = logging.getLogger(__name__)

# Allowed columns map (CSV Header -> DB Field)
COLUMN_MAP = inventory.IMPORT_COLUMN_MAP


def _normalize_header(name):
    return str(name).strip().lower().replace(' ', '_')


def _cell_text(value):
    """Spreadsheet cell -> the string a CSV export of it would hold
    (None for empty; 12.0 -> '12'; dates as ISO)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _read_xlsx_rows(file_obj):
    """Rows of the first worksheet as dicts keyed by normalized header.
    Entirely empty rows are skipped."""
    import openpyxl
    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            return []
        keys = [_normalize_header(h) if h is not None else None for h in header]
        records = []
        for values in rows:
            if all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
                continue
            records.append({k: _cell_text(v) for k, v in zip(keys, values) if k})
        return records
    finally:
        wb.close()

@inventory_bp.route('/import', methods=['GET', 'POST'])
@require_permission('inventory.manage')
def import_items():
    """Import inventory items from CSV or Excel."""
    if request.method == 'POST':
        file = request.files.get('file')
        update_existing = request.form.get('update_existing') == 'on'
        
        if not file or (not file.filename.endswith('.csv') and not file.filename.endswith('.xlsx')):
            flash("Please upload a valid CSV or Excel file.")
            return redirect(url_for('inventory.import_items'))
        
        try:
            items_to_process = []
            
            # Parse File
            if file.filename.endswith('.xlsx'):
                # Headers normalized: strip, lower, spaces -> underscores
                items_to_process = _read_xlsx_rows(io.BytesIO(file.stream.read()))
            else:
                # CSV parsing (utf-8-sig: tolerate the BOM Excel writes)
                stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
                csv_input = csv.DictReader(stream)
                # No clean header normalization in basic DictReader, handled in loop mostly?
                # Actually, our loop logic below relies on keys.
                # Let's standardize the CSV DictReader to behave like our DF dicts
                csv_data = list(csv_input)
                # Manual header normalization for CSV to match our new logic
                if csv_input.fieldnames:
                    norm_map = {k: k.strip().lower().replace(' ', '_') for k in csv_input.fieldnames}
                    for row in csv_data:
                        new_row = {norm_map[k]: v for k, v in row.items() if k in norm_map}
                        items_to_process.append(new_row)

            conn = get_db_connection()
            try:
                result = inventory.import_items(conn, items_to_process, update_existing,
                                                current_user.username)
            finally:
                conn.close()
            success_count, updated_count, skip_count, errors = result

            flash(f"Import Complete: {success_count} added, {updated_count} updated, {skip_count} skipped.")
            if errors:
                flash(f"Errors: {'; '.join(errors[:5])}...", "warning")
                
            return redirect(url_for('inventory.list_items'))
            
        except Exception as e:
            logger.error(f"Import error: {e}")
            flash(f"Error processing import: {e}")
            return redirect(url_for('inventory.import_items'))
            
    return render_template('inventory/import.html')

@inventory_bp.route('/import/template')
def download_import_template():
    """Download CSV template for imports."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header row
    fields = ['SKU', 'Name', 'Quantity', 'Cost', 'Price', 'Area', 'Aisle', 'Shelf', 'Bin', 'UPC', 'Part Number', 'Keywords', 'Supplier']
    writer.writerow(fields)
    
    # Sample row
    writer.writerow(['SAMPLE-SKU-01', 'Sample Item Name', '10', '5.00', '19.99', 'A', '1', 'B', '12', '0123456789', 'PN-123', 'sample, test', 'Supplier Inc'])
    
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=inventory_import_template.csv'}
    )
