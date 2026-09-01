from pathlib import Path
import csv, sqlite3, random, shutil, uuid
from datetime import date, datetime
ROOT=Path(r'C:\Users\wonde\MarginMise Restaurants\Barrel & Flame Bar + Grill'); OUT=ROOT/'Mock Historical Uploads'; UP=Path(r'C:\Users\wonde\Desktop\Barrel & Flame Bar + Grill - Auto Upload')
c=sqlite3.connect(ROOT/'restaurant_costs.sqlite3'); c.row_factory=sqlite3.Row
wanted=['Chicken wings','Cheddar cheese','Tomatoes','Romaine lettuce','Hamburger buns','French fries','Tortilla chips','Lime juice','Cola syrup 5','Silver tequila','House vodka']
items={}
for n in wanted:
 r=c.execute('select * from items where lower(item_name)=lower(?) order by item_id limit 1',(n,)).fetchone()
 if r: items[n]=r
print('Matched',len(items),'of',len(wanted))
# Correct inventory counts with real database identifiers and realistic values.
rows=[['Count Date','Item ID','Vendor','Vendor SKU','Item Name','Category','Count Unit','Counted Quantity','Unit Cost','Inventory Value','Notes']]
start=date(2024,1,1); end=date(2026,8,31); random.seed(9901)
import calendar
for y in range(2024,2027):
 for mth in range(1,13):
  if date(y,mth,1)>end: continue
  last=date(y,mth,calendar.monthrange(y,mth)[1])
  for n,r in items.items():
   q=round(random.uniform(3,18)*(0.25 if random.random()<.12 else 1),2); cost=float(r['current_price'] or 0); rows.append([last.isoformat(),r['item_id'],r['vendor_name'] or 'Mock Vendor',r['vendor_sku'],r['item_name'],r['category'],r['count_unit'] or 'count units',q,cost,round(q*cost,2),'Synthetic month-end physical count'])
with (OUT/'MOCK_UPLOAD_INVENTORY_MATCHED.csv').open('w',newline='',encoding='utf-8-sig') as f: csv.writer(f).writerows(rows)
recipes={'Flame Burger':['Hamburger buns','Cheddar cheese','Tomatoes','Romaine lettuce'],'Buffalo Wings':['Chicken wings'],'Fish Tacos':['Tortilla chips','Tomatoes','Lime juice'],'Loaded Fries':['French fries','Cheddar cheese'],'House Margarita':['Silver tequila','Lime juice'],'Draft Beer':['Cola syrup 5'],'Chicken Sandwich':['Chicken wings','Hamburger buns'],'Steak Frites':['French fries']}
rrows=[['POS Item Key','Menu Item Name','Inventory Item ID','Inventory Item Name','Quantity Count Units','Unit','Ingredient Cost']]
for menu,ings in recipes.items():
 for n in ings:
  r=items[n]; qty=.06 if n in ('Tomatoes','Romaine lettuce','Lime juice') else .08; cost=float(r['current_price'] or 0)/max(float(r['units_per_purchase_unit'] or 1),1)*qty
  rrows.append([menu.upper().replace(' ','_'),menu,r['item_id'],r['item_name'],qty,r['count_unit'] or 'count units',round(cost,2)])
import openpyxl
wb=openpyxl.Workbook(); ws=wb.active; ws.title='Recipes'
for row in rrows: ws.append(row)
wb.save(OUT/'MOCK_UPLOAD_RECIPE_MATCHED.xlsx')
shutil.copy2(OUT/'MOCK_UPLOAD_RECIPE_MATCHED.xlsx',UP/'MOCK_UPLOAD_RECIPE_MATCHED.xlsx'); shutil.copy2(OUT/'MOCK_UPLOAD_INVENTORY_MATCHED.csv',UP/'MOCK_UPLOAD_INVENTORY_MATCHED.csv')
print('Generated',len(rows)-1,'inventory rows and',len(rrows)-1,'recipe rows')
