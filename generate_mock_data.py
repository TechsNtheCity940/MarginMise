from pathlib import Path
import csv, random, math, json
from datetime import date, timedelta
random.seed(42026)
ROOT=Path(r"C:\Users\wonde\MarginMise Restaurants\Barrel & Flame Bar + Grill")
OUT=ROOT/"Mock Historical Uploads"
UPLOAD=ROOT/"Upload Invoices"
OUT.mkdir(exist_ok=True); UPLOAD.mkdir(exist_ok=True)
start=date(2024,1,1); end=date(2026,8,31)
items=[('Chicken Wings','Dry goods',92),('Cheddar Cheese','Dairy',118),('Tomatoes','Produce',36),('Romaine Lettuce','Produce',28),('Hamburger Buns','Dry goods',38),('French Fries','Dry goods',34),('Tortilla Chips','Dry goods',29),('Limes','Produce',32),('Lager Beer','Beverage',52),('House Vodka','alcohol',84),('Tequila','alcohol',96),('Cola Syrup','Beverage',72)]
menus=[('Flame Burger',14),('Buffalo Wings',13),('Fish Tacos',16),('Loaded Fries',11),('House Margarita',12),('Draft Lager',7),('Chicken Sandwich',15),('Steak Frites',24)]
def write_csv(path, header, rows):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(header); w.writerows(rows)
# Daily POS sales: seasonal + weekend + event/weather effects, with a few intentionally weak weather days.
rows=[]; d=start
while d<=end:
    dow=d.weekday(); month=d.month
    base=4200 if dow<4 else 6100 if dow==4 else 7600 if dow==5 else 5200
    season=1.0 + 0.10*math.sin((month-2)*math.pi/6)
    holiday=1.18 if (month,d.day) in [(2,14),(7,4),(11,23),(12,24),(12,31)] else 1.0
    rain=random.random()<0.17; temp=68+18*math.sin((month-1)*math.pi/6)+random.gauss(0,6)
    weather=0.82 if rain and dow<5 else 0.94 if rain else 1.0
    event=1.0
    if d.day in (7,21) and dow>=4: event=1.14
    sales=max(1200,base*season*holiday*weather*event+random.gauss(0,280))
    cogs=sales*random.uniform(.105,.145); labor=sales*.30
    rows.append([d.isoformat(),round(sales,2),round(cogs,2),round(labor,2),int(sales/25),int(sales/18),int(sales/16),int(sales/30)])
    d+=timedelta(days=1)
write_csv(OUT/'mock_daily_sales_2024_2026.csv',['Business Date','Net Sales','Theoretical COGS','Estimated Labor','Flame Burger Qty','Buffalo Wings Qty','Fish Tacos Qty','Loaded Fries Qty'],rows)
# Product purchases by month. Purchases are intentionally larger than usage in some months to test COGS logic.
monthly={}; d=start
while d<=end:
    key=(d.year,d.month); monthly[key]=monthly.get(key,0)+1; d+=timedelta(days=1)
inv=[]; d=start
while d<=end:
    if d.day in (2,9,16,23):
        for name,cat,case_price in items:
            qty=random.randint(1,5); inv.append([d.isoformat(),f'VEND-{items.index((name,cat,case_price))+1:03d}',name,cat,qty,case_price,round(qty*case_price,2)])
    d+=timedelta(days=1)
write_csv(OUT/'mock_purchases_2024_2026.csv',['Invoice Date','Vendor SKU','Item Name','Category','Quantity Cases','Unit Cost','Extended Cost'],inv)
# Waste records, concentrated on produce and wings to teach MarginMemory that usage is not all clean consumption.
waste=[]; d=start
while d<=end:
    if d.day in (5,15,25):
        for name,cat,_ in random.sample(items,3):
            q=round(random.uniform(.2,2.0),2); reason=random.choice(['Spoilage','Prep waste','Overproduction','Damaged'])
            waste.append([d.isoformat(),name,q,'count units',reason,round(q*random.uniform(3,8),2)])
    d+=timedelta(days=1)
write_csv(OUT/'mock_waste_2024_2026.csv',['Waste Date','Item Name','Quantity','Unit','Reason','Estimated Cost'],waste)
# Monthly operating costs and payroll snapshots.
op=[]; d=date(2024,1,1)
while d<=end:
    days=32-d.day if d.month==12 else 1
    nextm=(d.replace(day=28)+timedelta(days=4)).replace(day=1)
    for label,base in [('Water',1450),('Electric',4200),('Gas',1800),('Internet',180),('Cable/TV',140)]:
        amt=base*random.uniform(.88,1.18); op.append([d.isoformat(),label,round(amt,2),'Mock historical operating expense'])
    d=nextm
write_csv(OUT/'mock_operating_costs_2024_2026.csv',['Period Start','Cost Type','Amount','Description'],op)
# Inventory counts: month-end on-hand values, with occasional deliberate low-stock periods.
counts=[]
for y in range(2024,2027):
    for m in range(1,13):
        if y==2026 and m>8: continue
        last=(date(y,m+1,1)-timedelta(days=1)) if m<12 else date(y,12,31)
        for name,cat,_ in items:
            base=random.uniform(4,16); low=base*.28 if random.random()<.13 else base
            counts.append([last.isoformat(),name,round(low,2),'count units','Mock month-end physical count'])
write_csv(OUT/'mock_inventory_counts_2024_2026.csv',['Count Date','Item Name','On Hand','Unit','Notes'],counts)
# Weather + event history. Effects are embedded in sales above so learning has a known signal to discover.
weather=[]; events=[]; d=start
while d<=end:
    rain=random.random()<.17; temp=68+18*math.sin((d.month-1)*math.pi/6)+random.gauss(0,6)
    weather.append([d.isoformat(),round(temp+8,1),round(temp-8,1),random.randint(65,92),random.randint(0,80) if rain else random.randint(0,15),'Rain' if rain else 'Clear/Partly Cloudy'])
    if d.day in (7,21) and d.weekday()>=4:
        events.append([d.isoformat(),d.isoformat(),f'Barrel & Flame Live Music Night','Concert/Live Music',14,'Mock local event'])
    if (d.month,d.day) in [(2,14),(7,4),(11,23),(12,24),(12,31)]:
        events.append([d.isoformat(),d.isoformat(),['Valentine’s Day','Independence Day','Thanksgiving','Christmas Eve','New Year’s Eve'][[(2,14),(7,4),(11,23),(12,24),(12,31)].index((d.month,d.day))],'Holiday',18,'Mock holiday'])
    d+=timedelta(days=1)
write_csv(OUT/'mock_weather_2024_2026.csv',['Date','High F','Low F','Humidity %','Precipitation %','Condition'],weather)
write_csv(OUT/'mock_events_2024_2026.csv',['Event Date','End Date','Event Name','Category','Expected Impact %','Source'],events)
# POS item detail, enough volume to test recipe-driven theoretical COGS and menu learning.
detail=[]; d=start
while d<=end:
    sales=next(r[1] for r in rows if r[0]==d.isoformat())
    for name,price in menus:
        qty=max(0,int(sales/len(menus)/max(price,1)*random.uniform(.55,1.45)))
        detail.append([d.isoformat(),name,qty,round(qty*price,2),round(price,2)])
    d+=timedelta(days=1)
write_csv(OUT/'mock_pos_item_detail_2024_2026.csv',['Business Date','Menu Item','Quantity','Gross Sales','Menu Price'],detail)
# A compact recipe workbook with explicit inventory IDs/SKUs, useful for re-testing the Excel importer.
recipe_rows=[['POS Item Key','Menu Item Name','Inventory Item ID','Inventory Item Name','Quantity Count Units','Unit','Ingredient Cost']]
recipe_map={'Flame Burger':['Hamburger Buns','Cheddar Cheese','Tomatoes','Romaine Lettuce'],'Buffalo Wings':['Chicken Wings'],'Fish Tacos':['Tortilla Chips','Tomatoes','Limes'],'Loaded Fries':['French Fries','Cheddar Cheese'],'House Margarita':['Tequila','Limes'],'Draft Lager':['Lager Beer'],'Chicken Sandwich':['Chicken Wings','Hamburger Buns'],'Steak Frites':['French Fries']}
for menu,ings in recipe_map.items():
    for ing in ings:
        row=next(x for x in items if x[0]==ing); sku=f'VEND-{items.index(row)+1:03d}'
        qty=.05 if ing in ('Tomatoes','Romaine Lettuce','Limes') else .08
        recipe_rows.append([menu.upper().replace(' ','_'),menu,sku,ing,qty,'count units',round(row[2]*qty,2)])
# Build an XLSX with the same schema used by the recipe importer, plus a README manifest.
try:
    import openpyxl
    wb=openpyxl.Workbook(); ws=wb.active; ws.title='Recipes'
    for r in recipe_rows: ws.append(r)
    wb.create_sheet('Read Me').append(['MOCK DATA','Synthetic training/QA data for Barrel & Flame Bar + Grill','2024-2026'])
    wb.save(OUT/'mock_recipe_guide.xlsx')
except Exception as exc: print('XLSX skipped:',exc)
manifest={'restaurant':'Barrel & Flame Bar + Grill','period':f'{start} through {end}','synthetic':True,'purpose':'QA only; not real financial records','files':[p.name for p in OUT.iterdir()]}
(OUT/'MOCK_DATA_README.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print('Generated',len(rows),'daily sales rows,',len(inv),'purchase rows,',len(waste),'waste rows,',len(op),'operating cost rows,',len(counts),'inventory counts,',len(weather),'weather rows,',len(events),'events,',len(detail),'POS detail rows')
print('Output:',OUT)
