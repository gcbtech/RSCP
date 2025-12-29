# RSCP User Manual & Workflow Guide

Welcome to **RSCP (Receive, Scan, Check, Process)** v2.1.18.

This guide is designed to take you from a first-time user to a power user. RSCP is not just a collection of tools; it is a **unified operating system** for your resale or retail business. By integrating Receiving, Inventory, and Point of Sale into one platform, you eliminate data double-entry, reduce errors, and save hours of administrative work every week.

---

## 🚀 Why RSCP? The Efficiency Advantage

**The Old Way (Disconnected Systems):**
1.  Order item on Amazon/eBay.
2.  Manually type purchase details into a "Purchases" spreadsheet.
3.  Wait for package. Forget what's inside.
4.  Package arrives. Search emails to find out what it is.
5.  Open "Inventory" spreadsheet. Type in item name, cost, and location.
6.  Sell item. Open spreadsheet again to deduct quantity.
7.  Write a receipt by hand or use a separate card terminal calculator.

**The RSCP Way:**
1.  **Forward Email**: Forward your shipping confirmation to RSCP.
2.  **Scan**: Package arrives. Scan the barcode. RSCP tells you exactly what it is.
3.  **Click**: One click adds it to Inventory. Fill in deatils such as price, cost, and location.
4.  **Sell**: Scan item at POS. Receipt prints. Inventory updates automatically.

**Result**: You touch the data *once*. The system handles the rest.

---

## 📦 Module 1: Receiving (The Funnel)

The Receiving module is your "Inbox" for physical goods. It tracks everything coming into your business.

### Key Functions
*   **Dashboard**: A high-level view of your pipeline. See what's **Expected**, **Pending** (arrived but not processed), and **Past Due**.
*   **Email Ingestion**: Forward your Amazon/eBay "Shipped" emails to your RSCP email address. The system automatically extracts tracking numbers and item names.
*   **Scan & Receive**: Use a barcode scanner (or mobile camera) to scan incoming tracking numbers.
    *   *Found*: System marks it as "Received" and shows you what's inside.
    *   *Unknown*: System prompts you to add a new manual entry on the fly.
*   **Returns Management**: Track items you are sending back. Log return reasons (Defective, Wrong Item) and track refund status.

---

## 📋 Module 2: Inventory (The Warehouse)

Once a package is received, the items inside need to be organized. This is your "Source of Truth."

### Key Functions
*   **Inventory List**: A searchable, sortable grid of every item you own. Filter by Category, Location, or Stock Status.
*   **Quick Add**: The fastest way to stock items.
    *   *Auto-SKU*: Generates a unique ID automatically.
*   **Location Tracking**: Never lose an item again. Assign items to specific 4-level locations:
    *   **Area**: Front / Back Room
    *   **Aisle**: Aisle 1 / Aisle 2
    *   **Shelf**: Top / Middle / Bottom
    *   **Bin**: Bin A / Bin B
*   **Stock Alerts**: define a "Low Stock Threshold" (e.g., 5 units). When you dip below this, RSCP sends a webhook alert to your chat app (Slack/Discord), as well as displaying them on the Inventory Overview page.

---

## 💳 Module 3: Point of Sale (The Register)

The POS is where you monetize your inventory. It is optimized for speed and works with touchscreens and barcode scanners.

### Key Functions
*   **Checkout**:
    *   **Scan to Cart**: Zap an item's SKU barcode to add it instantly.
    *   **Smart Search**: Type "cable" to see all cables and add them.
    *   **Quick Add (Misc)**: Selling an untracked service? Add a custom line item on the fly.
*   **Discounts & Taxes**:
    *   Apply percentage or fixed-amount discounts.
    *   Manager Override: High-value discounts require a manager PIN.
    *   Automatic Sales Tax calculation based on your settings.
*   **Money Management**:
    *   **End of Day**: reconcile cash, card, and other payments. The system generates a "Z-Report" ensuring your drawer balances perfectly.
*   **Receipts**: Print professional receipts on standard 58mm/80mm thermal printers

---

## 🌟 Start-to-Finish Example: The "Golden Workflow"

Let's walk through a real-world scenario: **Selling USB-C Cables**.

### Step 1: Purchasing
1.  You order 50 generic USB-C cables from a supplier on eBay.
2.  The supplier sends a shipping notification email with tracking number `94001000...`.
3.  **Action**: You auto-forward this email to RSCP.
4.  **Result**: RSCP creates a "Pending Package" record. The Dashboard shows "1 Package Expected".

### Step 2: Receiving & Stocking
1.  The box arrives at your door a few days later.
2.  **Action**: You grab your barcode scanner and zap the shipping label `94001000...`.
3.  **Result**:
    *   RSCP beeps and flashes **GREEN**.
    *   The screen says: "Received: 50x USB-C Cables (eBay Order #123)".
    *   A button appears: **"Add to Inventory"**.
4.  **Action**: You click "Item Name" from the package to pre-fill the form.
    *   **Category**: "Electronics"
    *   **Cost**: $2.00
    *   **Price**: $10.00
    *   **Location**: Aisle 1, Bin 4
    *   **Quantity**: 50
5.  **Action**: Click **Save**.
6.  **Result**: You now have 50 units in stock. The package is archived.

### Step 3: Selling (The Payoff)
1.  A customer walks into your shop. "I need a phone charger."
2.  You walk to Aisle 1, Bin 4 (because RSCP told you exactly where they are), and grab a cable.
3.  **Action**: At the POS counter, you scan the cable's SKU barcode.
4.  **Result**:
    *   Item hits the cart: **USB-C Cable - $10.00**.
    *   Tax is auto-added ($0.70).
    *   Total: $10.70.
5.  **Action**: Customer pays with a Card. You tap "Card" -> "Pay".
6.  **Result**:
    *   Receipt prints automatically (with your store logo).
    *   Inventory count drops from **50** to **49** instantly.
    *   The sale is logged in the Daily Report.

---

## 💡 Tips for Efficiency

*   **Keyboard Shortcuts**:
    *   In Lists: Use `J` / `K` to move up/down rows.
    *   POS: `F2` focuses the search bar. `Enter` completes a cash sale.
*   **Dark Mode**: Working late? Toggle Dark Mode in the settings menu to save your eyes.
*   **Mobile Scanner**: Compatible with industry standard Android barcode scanners. Open RSCP on your scanner and use the built-in barcode reader to scan packages and inventory items as you walk.
---
*Generated for RSCP v2.1.18*
