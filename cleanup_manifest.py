import pandas as pd

df = pd.read_csv('manifest.csv')
print(f"Before: {len(df)} rows")

# For eBay format, remove rows where TrackingNumber is an Order ID and TrackingNumber.1 is empty
# Order ID pattern: XX-XXXXX-XXXXX
order_id_pattern = r'^\d{2}-\d{5}-\d{5}$'

# Check if this is eBay format (has TrackingNumber.1)
if 'TrackingNumber.1' in df.columns:
    # Keep rows where EITHER:
    # 1. TrackingNumber.1 has a real tracking number, OR
    # 2. TrackingNumber doesn't look like an Order ID
    df['is_order_id'] = df['TrackingNumber'].astype(str).str.match(order_id_pattern, na=False)
    df['has_real_tracking'] = df['TrackingNumber.1'].astype(str).str.strip().ne('') & df['TrackingNumber.1'].notna()
    
    # Remove: Order ID in TrackingNumber AND no real tracking in TrackingNumber.1
    to_remove = df['is_order_id'] & ~df['has_real_tracking']
    print(f"Rows to remove: {to_remove.sum()}")
    
    df = df[~to_remove]
    df = df.drop(columns=['is_order_id', 'has_real_tracking'])
    
    df.to_csv('manifest.csv', index=False)
    print(f"After: {len(df)} rows")
else:
    print("Not eBay format, no changes needed")
