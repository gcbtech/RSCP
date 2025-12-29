#!/usr/bin/env python3
"""
Test File Generator for RSCP Import Preview Testing
Generates various CSV and XLSX files with different column layouts and data.
"""
import random
import string
import os
from datetime import datetime, timedelta

# Try to import pandas and openpyxl
try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas is required. Install with: pip install pandas")
    exit(1)

try:
    import openpyxl
except ImportError:
    print("WARNING: openpyxl not installed. XLSX files won't be generated.")
    print("Install with: pip install openpyxl")
    openpyxl = None

OUTPUT_DIR = "test_imports"

# Sample data pools
ITEM_NAMES = [
    "Apple AirPods Pro (2nd Generation)",
    "Samsung Galaxy S23 Ultra Case",
    "Anker USB-C Charger 65W",
    "Logitech MX Master 3S Mouse",
    "Sony WH-1000XM5 Headphones",
    "Nintendo Switch OLED Controller",
    "Kindle Paperwhite 11th Gen",
    "Fire TV Stick 4K Max",
    "Echo Dot (5th Gen) Smart Speaker",
    "Ring Video Doorbell Pro 2",
    "Bose QuietComfort Earbuds II",
    "JBL Flip 6 Portable Speaker",
    "Fitbit Charge 5 Fitness Tracker",
    "GoPro HERO11 Black",
    "DJI Mini 3 Pro Drone",
    "Instant Pot Duo 7-in-1",
    "Ninja Air Fryer Max XL",
    "Dyson V15 Detect Vacuum",
    "iRobot Roomba j7+",
    "Vitamix E310 Blender",
]

CARRIERS = ["UPS", "USPS", "FedEx", "Amazon", "DHL", "OnTrac"]


def generate_tracking_number(carrier=None):
    """Generate realistic-looking tracking numbers."""
    carrier = carrier or random.choice(CARRIERS)
    
    if carrier == "UPS":
        # 1Z + 6 alphanumeric + 2 digits + 8 digits
        return f"1Z{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}{''.join(random.choices(string.digits, k=10))}"
    elif carrier == "USPS":
        # 9400 + 18 digits or 9200 + 18 digits
        prefix = random.choice(["9400", "9200", "9261", "9407"])
        return f"{prefix}{''.join(random.choices(string.digits, k=18))}"
    elif carrier == "FedEx":
        # 12 or 15 digits
        length = random.choice([12, 15])
        return ''.join(random.choices(string.digits, k=length))
    elif carrier == "Amazon":
        # TBA + 12 digits
        return f"TBA{''.join(random.choices(string.digits, k=12))}"
    elif carrier == "DHL":
        # 10 digits
        return ''.join(random.choices(string.digits, k=10))
    else:
        # Generic 15 alphanumeric
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=15))


def generate_asin():
    """Generate Amazon ASIN (10 characters, starts with B0)."""
    return f"B0{''.join(random.choices(string.ascii_uppercase + string.digits, k=8))}"


def generate_date(offset_range=(-30, 30)):
    """Generate a date within offset range from today."""
    today = datetime.now()
    offset = random.randint(offset_range[0], offset_range[1])
    return (today + timedelta(days=offset)).strftime("%Y-%m-%d")


def generate_rows(count=50):
    """Generate random package data."""
    rows = []
    for _ in range(count):
        carrier = random.choice(CARRIERS)
        rows.append({
            "tracking": generate_tracking_number(carrier),
            "name": random.choice(ITEM_NAMES),
            "date": generate_date(),
            "quantity": random.randint(1, 3),
            "asin": generate_asin() if random.random() > 0.3 else "",
            "image": f"https://example.com/img/{random.randint(1000, 9999)}.jpg" if random.random() > 0.5 else "",
            "carrier": carrier,
        })
    return rows


def create_test_file(filename, columns, data, file_format="csv"):
    """Create a test file with specified columns and data."""
    df = pd.DataFrame(data)
    
    # Rename columns to match the specified column names
    column_map = {}
    for target, source in columns.items():
        if source in df.columns:
            column_map[source] = target
    
    df = df.rename(columns=column_map)
    
    # Only keep specified columns
    df = df[[c for c in columns.keys() if c in df.columns]]
    
    filepath = os.path.join(OUTPUT_DIR, filename)
    
    if file_format == "xlsx":
        if openpyxl:
            df.to_excel(filepath, index=False)
            print(f"  ✅ Created: {filename}")
        else:
            print(f"  ⚠️ Skipped XLSX (openpyxl not installed): {filename}")
    else:
        df.to_csv(filepath, index=False)
        print(f"  ✅ Created: {filename}")


def main():
    print("\n" + "="*60)
    print("RSCP Test File Generator")
    print("="*60)
    
    # Create output directory
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    
    # Generate base data
    data_small = generate_rows(15)
    data_medium = generate_rows(50)
    data_large = generate_rows(200)
    
    print(f"\nGenerating test files in: {os.path.abspath(OUTPUT_DIR)}/")
    print("-"*60)
    
    # Test 1: Standard Amazon format
    print("\n📦 Standard Formats:")
    create_test_file("standard_amazon.csv", {
        "Tracking number": "tracking",
        "Item name": "name",
        "Expected delivery date": "date",
        "Quantity": "quantity",
        "ASIN": "asin",
    }, data_medium)
    
    # Test 2: eBay-style format
    create_test_file("ebay_style.csv", {
        "Carrier Tracking #": "tracking",
        "Title": "name",
        "Ship Date": "date",
    }, data_medium)
    
    # Test 3: Minimal - just tracking
    create_test_file("minimal_tracking_only.csv", {
        "Tracking": "tracking",
    }, data_small)
    
    # Test 4: Different column names
    print("\n🔀 Unusual Column Names:")
    create_test_file("unusual_columns.csv", {
        "Package ID Number": "tracking",
        "Product Description": "name",
        "Estimated Arrival": "date",
        "Photo URL": "image",
    }, data_medium)
    
    # Test 5: Columns in different order
    create_test_file("reversed_order.csv", {
        "Image URL": "image",
        "Qty": "quantity",
        "Order Date": "date",
        "Description": "name",
        "Tracking Number": "tracking",
    }, data_medium)
    
    # Test 6: With priority/extra columns
    create_test_file("with_extras.csv", {
        "Tracking #": "tracking",
        "Item Title": "name",
        "Promise Date": "date",
        "Product ASIN": "asin",
        "Carrier": "carrier",
        "Units": "quantity",
    }, data_large)
    
    # Test 7: Missing tracking column (should warn)
    print("\n⚠️ Edge Cases (should show warnings):")
    create_test_file("missing_tracking.csv", {
        "Product Name": "name",
        "Purchase Date": "date",
        "Image": "image",
    }, data_small)
    
    # Test 8: Empty dates
    data_no_dates = generate_rows(20)
    for row in data_no_dates:
        row["date"] = ""
    create_test_file("empty_dates.csv", {
        "Tracking number": "tracking",
        "Item name": "name",
        "Empty Date Column": "date",
    }, data_no_dates)
    
    # Test 9: Some empty tracking numbers
    data_some_empty = generate_rows(30)
    for i in range(0, len(data_some_empty), 5):
        data_some_empty[i]["tracking"] = ""
    create_test_file("some_empty_tracking.csv", {
        "Tracking number": "tracking",
        "Item name": "name",
        "Date": "date",
    }, data_some_empty)
    
    # XLSX Tests
    if openpyxl:
        print("\n📊 Excel (.xlsx) Files:")
        create_test_file("excel_standard.xlsx", {
            "Tracking Number": "tracking",
            "Item Name": "name",
            "Expected Delivery Date": "date",
            "ASIN": "asin",
        }, data_medium, file_format="xlsx")
        
        create_test_file("excel_large.xlsx", {
            "Carrier Tracking Number": "tracking",
            "Title": "name",
            "Order Date": "date",
            "Qty": "quantity",
            "Image URL": "image",
        }, data_large, file_format="xlsx")
    
    print("\n" + "="*60)
    print(f"✅ Done! Files created in: {os.path.abspath(OUTPUT_DIR)}/")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
