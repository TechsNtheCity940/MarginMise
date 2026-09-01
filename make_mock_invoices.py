from pathlib import Path
import csv
ROOT=Path(r'C:\Users\wonde\MarginMise Restaurants\Barrel & Flame Bar + Grill'); src=ROOT/'Mock Historical Uploads'/'mock_purchases_2024_2026.csv'; out=ROOT/'Mock Historical Uploads'/'MOCK_UPLOAD_PURCHASE_INVOICES_2024_2026.csv'; rows=[]
with src.open(encoding='utf-8-sig',newline='') as f:
 for i,r in enumerate(csv.DictReader(f),1):
  vendor={'Dry goods':'Heartland Restaurant Supply','Dairy':'FreshRoute Foods','Produce':'Green Acres Produce','Beverage':'Gulf Coast Beverage','alcohol':'Southern Spirits Distributors'}.get(r['Category'],'Heartland Restaurant Supply')
  rows.append([f'MOCKINV-{r["Invoice Date"].replace("-","")}-{(i-1)//3:05d}',vendor,r['Invoice Date'],r['Item Name'],r['Quantity Cases'],r['Unit Cost'],r['Extended Cost']])
with out.open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.writer(f); w.writerow(['Invoice Number','Vendor','Invoice Date','Vendor SKU','Item Description','Quantity','Unit Price','Line Total']); w.writerows([[r[0],r[1],r[2],src_row['Vendor SKU'],r[3],r[4],r[5],r[6]] for r,src_row in zip(rows,[x for x in csv.DictReader(src.open(encoding='utf-8-sig'))])])
print('invoice rows',len(rows),'unique invoices',len(set(r[1] for r in rows)))
