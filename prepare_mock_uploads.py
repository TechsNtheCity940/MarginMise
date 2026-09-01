from pathlib import Path
import csv, shutil, uuid
from datetime import date, datetime
ROOT=Path(r'C:\Users\wonde\MarginMise Restaurants\Barrel & Flame Bar + Grill')
OUT=ROOT/'Mock Historical Uploads'; UP=ROOT/'Upload Invoices'
UP.mkdir(exist_ok=True)
def read(name):
    with (OUT/name).open(encoding='utf-8-sig',newline='') as f: return list(csv.reader(f))
def write(name,rows):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f: csv.writer(f).writerows(rows)
# POS schema matches the production item-level importer.
s=read('mock_pos_item_detail_2024_2026.csv'); out=[['Business Date','Order ID','Location','Channel','Menu Item Name','Category','Quantity','Unit Price','Gross Sales','Discounts','Refunds','Net Sales','Sales Tax']]
for i,r in enumerate(s[1:],1):
    dt,name,qty,gross,price=r
    if not qty: continue
    cat='alcohol' if 'Margarita' in name else 'Beverage' if 'Lager' in name else 'Dry Goods'
    disc=round(float(gross)*0.02 if i%37==0 else 0,2); net=round(float(gross)-disc,2); tax=round(net*.0825,2)
    out.append([dt,f'MOCK-{dt.replace("-","")}-{i:05d}','Main Dining Room','Dine In',name,cat,qty,price,gross,disc,0,net,tax])
write('MOCK_UPLOAD_POS_2024_2026.csv',out)
# Inventory schema uses the exact fields consumed by inventory_planning.py.
c=read('mock_inventory_counts_2024_2026.csv'); out=[['Count Date','Item ID','Vendor','Vendor SKU','Item Name','Category','Count Unit','Counted Quantity','Unit Cost','Inventory Value','Notes']]
item_ids={'Chicken Wings':'ITM-0','Cheddar Cheese':'ITM-B28B3A6E5A23','Tomatoes':'ITM-1A4D2E8D0699','Romaine Lettuce':'ITM-B0866F0D2261','Hamburger Buns':'ITM-F84FB041006D','French Fries':'ITM-FR','Tortilla Chips':'ITM-CBDDBBFD770B','Limes':'ITM-LIME','Lager Beer':'ITM-BEER','House Vodka':'ITM-VODKA','Tequila':'ITM-TEQ','Cola Syrup':'ITM-COLA'}
for r in c[1:]:
 dt,name,q,unit,note=r; row=next(x for x in __import__('builtins').items if x[0]==name) if False else None
 sku=f'VEND-{[x[0] for x in __import__("json").loads("[]")]}'; out.append([dt,item_ids.get(name,''),'Mock Vendor',f'MOCK-{name[:4].upper()}',name,'Dry goods' if name not in ('Cheddar Cheese','Lager Beer','House Vodka','Tequila','Limes','Tomatoes','Romaine Lettuce') else ('Dairy' if name=='Cheddar Cheese' else 'Produce' if name in ('Limes','Tomatoes','Romaine Lettuce') else 'Beverage' if name=='Lager Beer' else 'alcohol'),'count units',q,0,0,note])
write('MOCK_UPLOAD_INVENTORY_2024_2026.csv',out)
# Operating cost and waste headers match classifier/importer signatures.
op=read('mock_operating_costs_2024_2026.csv'); write('MOCK_UPLOAD_OPERATING_COSTS_2024_2026.csv',[['Date','Category','Description','Amount']]+[[r[0],r[1],r[3],r[2]] for r in op[1:]])
w=read('mock_waste_2024_2026.csv'); write('MOCK_UPLOAD_WASTE_2024_2026.csv',[['Waste Date','Item Name','Quantity Count Units','Count Unit','Reason','Estimated Cost']]+[[r[0],r[1],r[2],r[3],r[4],r[5]] for r in w[1:]])
# Event CSV is supported by the table router; keep it separate from the ICS calendar below.
e=read('mock_events_2024_2026.csv'); write('MOCK_UPLOAD_EVENTS_2024_2026.csv',[['Event Date','End Date','Event Name','Category','Expected Impact %','Source']]+e[1:])
# Recipe workbook is already compatible with the repaired Excel importer.
for name in ['MOCK_UPLOAD_POS_2024_2026.csv','MOCK_UPLOAD_OPERATING_COSTS_2024_2026.csv','MOCK_UPLOAD_INVENTORY_2024_2026.csv','MOCK_UPLOAD_WASTE_2024_2026.csv','MOCK_UPLOAD_EVENTS_2024_2026.csv','mock_recipe_guide.xlsx']:
    src=OUT/name; dst=UP/name
    shutil.copy2(src,dst)
# Create an ICS file for event-calendar ingestion.
ics=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//MarginMise Mock QA//EN']
for r in e[1:]:
    dt=datetime.strptime(r[0],'%Y-%m-%d').strftime('%Y%m%d'); uid=str(uuid.uuid4())+'@marginmise.mock'
    ics += ['BEGIN:VEVENT',f'UID:{uid}',f'DTSTART;VALUE=DATE:{dt}',f'DTEND;VALUE=DATE:{dt}',f'SUMMARY:{r[2]}','DESCRIPTION:Synthetic QA event; do not use for financial reporting.','END:VEVENT']
ics.append('END:VCALENDAR'); (OUT/'MOCK_UPLOAD_EVENTS_2024_2026.ics').write_text('\n'.join(ics)+'\n',encoding='utf-8'); shutil.copy2(OUT/'MOCK_UPLOAD_EVENTS_2024_2026.ics',UP/'MOCK_UPLOAD_EVENTS_2024_2026.ics')
print('Prepared upload files in',UP)
