import io, os, re, json, math, unicodedata, zipfile
from datetime import date, datetime
from pathlib import Path
from collections import defaultdict, Counter

import pandas as pd
import streamlit as st
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, Flowable
from reportlab.lib.utils import ImageReader

try:
    import psycopg2
    from psycopg2.extras import Json
except Exception:
    psycopg2 = None

APP_DIR = Path(__file__).parent
LOGO_PATH = Path(__file__).parent / "logo_raiz_latina_mayoristas.png"
try:
    SECRET_DB_URL = st.secrets.get('DATABASE_URL', '')
except Exception:
    SECRET_DB_URL = ''
DB_URL = os.getenv('DATABASE_URL') or os.getenv('NEON_DATABASE_URL') or SECRET_DB_URL
LOCAL_DB = APP_DIR / 'distribuidor_logistico.db'

st.set_page_config(page_title='RAÍZ LATINA · Distribuidor Logístico', page_icon='📦', layout='wide')

# ---------------- DB ----------------
def db_conn():
    if DB_URL and psycopg2:
        return psycopg2.connect(DB_URL, sslmode='require')
    import sqlite3
    return sqlite3.connect(LOCAL_DB)

def is_pg(conn):
    return psycopg2 is not None and conn.__class__.__module__.startswith('psycopg2')

def init_db():
    conn = db_conn(); cur = conn.cursor()
    if is_pg(conn):
        cur.execute("""CREATE TABLE IF NOT EXISTS catalog_products (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            weight_kg DOUBLE PRECISION NOT NULL DEFAULT 0,
            price TEXT,
            raw_data JSONB NOT NULL,
            imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        cur.execute('ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE')
        cur.execute('ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()')
        cur.execute('ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()')
        cur.execute("""CREATE TABLE IF NOT EXISTS catalog_meta (
            id INTEGER PRIMARY KEY CHECK (id=1), imported_at TIMESTAMPTZ,
            product_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, catalog_version TEXT
        )""")
        cur.execute('ALTER TABLE catalog_meta ADD COLUMN IF NOT EXISTS total_count INTEGER DEFAULT 0')
        cur.execute('ALTER TABLE catalog_meta ADD COLUMN IF NOT EXISTS catalog_version TEXT')
    else:
        cur.execute("""CREATE TABLE IF NOT EXISTS catalog_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, name_norm TEXT NOT NULL,
            weight_kg REAL NOT NULL DEFAULT 0, price TEXT, raw_data TEXT NOT NULL,
            imported_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
        )""")
        cols={r[1] for r in cur.execute('PRAGMA table_info(catalog_products)').fetchall()}
        now=datetime.now().isoformat(timespec='seconds')
        if 'active' not in cols: cur.execute('ALTER TABLE catalog_products ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        if 'first_seen_at' not in cols: cur.execute('ALTER TABLE catalog_products ADD COLUMN first_seen_at TEXT')
        if 'last_seen_at' not in cols: cur.execute('ALTER TABLE catalog_products ADD COLUMN last_seen_at TEXT')
        cur.execute('UPDATE catalog_products SET first_seen_at=COALESCE(first_seen_at, imported_at, ?), last_seen_at=COALESCE(last_seen_at, imported_at, ?)',(now,now))
        cur.execute("""CREATE TABLE IF NOT EXISTS catalog_meta (
            id INTEGER PRIMARY KEY, imported_at TEXT, product_count INTEGER DEFAULT 0,
            total_count INTEGER DEFAULT 0, catalog_version TEXT
        )""")
        mcols={r[1] for r in cur.execute('PRAGMA table_info(catalog_meta)').fetchall()}
        if 'total_count' not in mcols: cur.execute('ALTER TABLE catalog_meta ADD COLUMN total_count INTEGER DEFAULT 0')
        if 'catalog_version' not in mcols: cur.execute('ALTER TABLE catalog_meta ADD COLUMN catalog_version TEXT')
    conn.commit(); conn.close()

init_db()

def norm_text(s):
    s = '' if s is None else str(s)
    s = unicodedata.normalize('NFKD', s).encode('ascii','ignore').decode('ascii').lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    return re.sub(r'\s+', ' ', s)

def num_value(v):
    if v is None or str(v).strip() == '': return 0.0
    s = str(v).strip().replace(' ', '')
    # Tienda Nube export normally uses 123,456.78 or 123.456,78 depending locale.
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'): s=s.replace('.','').replace(',','.')
        else: s=s.replace(',','')
    elif ',' in s:
        s=s.replace(',','.')
    try: return float(s)
    except: return 0.0

def read_catalog(uploaded):
    raw = uploaded.getvalue()
    last_err = None
    for enc in ('utf-8-sig','cp1252','latin1'):
        try:
            df = pd.read_csv(io.BytesIO(raw), sep=';', encoding=enc, dtype=str, keep_default_na=False)
            if len(df.columns) >= 2: return df, enc
        except Exception as e: last_err=e
    raise ValueError(f'No pude leer el CSV de Tienda Nube. {last_err}')

def catalog_rows(active_only=True):
    conn=db_conn(); cur=conn.cursor()
    where = ''
    if active_only: where = ' WHERE active=TRUE' if is_pg(conn) else ' WHERE active=1'
    cur.execute('SELECT id,name,name_norm,weight_kg,price,raw_data FROM catalog_products'+where+' ORDER BY id')
    rows=cur.fetchall(); conn.close(); out=[]
    for r in rows:
        raw=r[5] if isinstance(r[5],dict) else json.loads(r[5])
        out.append({'id':r[0],'name':r[1],'name_norm':r[2],'weight_kg':float(r[3] or 0),'price':r[4] or '', 'raw_data':raw})
    return out

def catalog_meta():
    conn=db_conn(); cur=conn.cursor(); cur.execute('SELECT imported_at, product_count, total_count, catalog_version FROM catalog_meta WHERE id=1'); r=cur.fetchone(); conn.close(); return r

def format_imported_at(value):
    if not value: return '—'
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is not None: dt=dt.astimezone()
        return dt.strftime('%d/%m/%Y %H:%M')
    except Exception: return str(value)

def update_catalog(df):
    rows=[]
    for _,r in df.iterrows():
        raw={str(k): ('' if pd.isna(v) else str(v)) for k,v in r.to_dict().items()}
        name=raw.get('Nombre','').strip()
        if not name: continue
        rows.append((name,norm_text(name),num_value(raw.get('Peso (kg)')),raw.get('Precio',''),raw))
    conn=db_conn(); cur=conn.cursor()
    cur.execute('SELECT id,name,name_norm,weight_kg,price,raw_data FROM catalog_products ORDER BY id')
    old=[]
    for r in cur.fetchall():
        raw=r[5] if isinstance(r[5],dict) else json.loads(r[5])
        old.append({'id':r[0],'name':r[1],'name_norm':r[2],'weight_kg':float(r[3] or 0),'price':r[4] or '','raw_data':raw})
    by_exact=defaultdict(list); by_norm=defaultdict(list)
    for x in old: by_exact[x['name']].append(x); by_norm[x['name_norm']].append(x)
    now=datetime.now().isoformat(timespec='seconds'); version=now
    seen_ids=set(); new_count=updated=unchanged=0
    def same_data(o,name,norm,w,price,raw):
        return o['name']==name and o['name_norm']==norm and abs(o['weight_kg']-w)<1e-12 and o['price']==price and o['raw_data']==raw
    for name,norm,w,price,raw in rows:
        candidates=[x for x in by_exact.get(name,[]) if x['id'] not in seen_ids]
        candidate=candidates[0] if candidates else None
        if candidate is None:
            nc=[x for x in by_norm.get(norm,[]) if x['id'] not in seen_ids]
            if len(nc)==1: candidate=nc[0]
        if candidate is None:
            if is_pg(conn):
                cur.execute('''INSERT INTO catalog_products(name,name_norm,weight_kg,price,raw_data,imported_at,active,first_seen_at,last_seen_at) VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s,%s) RETURNING id''',(name,norm,w,price,Json(raw),now,now,now)); pid=cur.fetchone()[0]
            else:
                cur.execute('''INSERT INTO catalog_products(name,name_norm,weight_kg,price,raw_data,imported_at,active,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)''',(name,norm,w,price,json.dumps(raw,ensure_ascii=False),now,1,now,now)); pid=cur.lastrowid
            new_count+=1
        else:
            pid=candidate['id']
            if same_data(candidate,name,norm,w,price,raw): unchanged+=1
            else: updated+=1
            if is_pg(conn): cur.execute('''UPDATE catalog_products SET name=%s,name_norm=%s,weight_kg=%s,price=%s,raw_data=%s,imported_at=%s,active=TRUE,last_seen_at=%s WHERE id=%s''',(name,norm,w,price,Json(raw),now,now,pid))
            else: cur.execute('''UPDATE catalog_products SET name=?,name_norm=?,weight_kg=?,price=?,raw_data=?,imported_at=?,active=1,last_seen_at=? WHERE id=?''',(name,norm,w,price,json.dumps(raw,ensure_ascii=False),now,now,pid))
        seen_ids.add(pid)
    if is_pg(conn):
        if seen_ids: cur.execute('UPDATE catalog_products SET active=FALSE WHERE NOT (id = ANY(%s))',(list(seen_ids),))
        else: cur.execute('UPDATE catalog_products SET active=FALSE')
        cur.execute('SELECT COUNT(*) FROM catalog_products WHERE active=TRUE'); active_count=cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM catalog_products'); total_count=cur.fetchone()[0]
        cur.execute('''INSERT INTO catalog_meta(id,imported_at,product_count,total_count,catalog_version) VALUES(1,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET imported_at=EXCLUDED.imported_at,product_count=EXCLUDED.product_count,total_count=EXCLUDED.total_count,catalog_version=EXCLUDED.catalog_version''',(now,active_count,total_count,version))
    else:
        if seen_ids: cur.execute('UPDATE catalog_products SET active=0 WHERE id NOT IN (%s)' % ','.join('?' for _ in seen_ids),tuple(seen_ids))
        else: cur.execute('UPDATE catalog_products SET active=0')
        cur.execute('SELECT COUNT(*) FROM catalog_products WHERE active=1'); active_count=cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM catalog_products'); total_count=cur.fetchone()[0]
        cur.execute('INSERT OR REPLACE INTO catalog_meta(id,imported_at,product_count,total_count,catalog_version) VALUES(1,?,?,?,?)',(now,active_count,total_count,version))
    conn.commit(); conn.close()
    return {'total':active_count,'new':new_count,'updated':updated,'unchanged':unchanged,'historical':max(0,total_count-active_count),'version':version}

# ---------------- PDF ----------------
def extract_pdf(pdf_bytes):
    reader=PdfReader(io.BytesIO(pdf_bytes))
    text='\n'.join((p.extract_text() or '') for p in reader.pages)
    lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines() if x.strip()]
    m=re.search(r'Orden\s*#?\s*(\d+)', text, re.I)
    if not m: raise ValueError('No pude detectar el número de pedido dentro del PDF.')
    order_num=m.group(1)
    # Product section: after Producto Cant. until Subtotal.
    start=next((i for i,l in enumerate(lines) if l.lower().startswith('producto')),None)
    end=next((i for i,l in enumerate(lines) if l.lower().startswith('subtotal')),None)
    if start is None or end is None or end<=start: raise ValueError('No pude detectar la tabla de productos del pedido.')
    product_lines=lines[start+1:end]
    items=[]
    for l in product_lines:
        mm=re.match(r'^(.*?)(?:\s+)(\d+)\s*$',l)
        if not mm: continue
        name=mm.group(1).strip(); qty=int(mm.group(2))
        items.append({'name':name,'qty':qty})
    # Customer block
    send=next((i for i,l in enumerate(lines) if l.lower()=='enviar a:'),None)
    customer=''; phone=''; address_parts=[]; country=''
    if send is not None:
        block=lines[send+1:]
        if block: customer=block[0]
        for l in block[1:]:
            if l.lower().startswith('teléfono:') or l.lower().startswith('telefono:'): phone=l.split(':',1)[1].strip()
            elif l.lower() in ('medio de pago:','envío:'): break
            else: address_parts.append(l)
        # Country is last nonempty line in PDF after address
        if address_parts: country=address_parts[-1]
    return {'order_num':order_num,'items':items,'customer':customer,'phone':phone,'address_lines':address_parts,'country':country,'page_count':len(reader.pages),'raw_text':text}

# ---------------- Matching ----------------
def match_items(order_items, catalog):
    by_norm=defaultdict(list)
    for p in catalog: by_norm[p['name_norm']].append(p)
    resolved=[]; issues=[]
    for it in order_items:
        n=norm_text(it['name']); matches=by_norm.get(n,[])
        if len(matches)==1:
            p=matches[0]; resolved.append({**it,'product_id':p['id'],'catalog_name':p['name'],'weight_kg':p['weight_kg'],'price':p['price'],'raw_data':p['raw_data']})
        elif len(matches)>1:
            issues.append({'name':it['name'],'type':'ambiguous','candidates':[x['name'] for x in matches]})
        else:
            # Suggestions are informational only, never auto-selected.
            suggestions=[]
            for p in catalog:
                if n and (n in p['name_norm'] or p['name_norm'] in n): suggestions.append(p['name'])
                if len(suggestions)>=5: break
            issues.append({'name':it['name'],'type':'missing','candidates':suggestions})
    return resolved,issues

# ---------------- Packing ----------------
def box_limit(country):
    return (9.0,10.0) if norm_text(country) == 'el salvador' else (14.0,15.0)

def pack_items(resolved,country):
    max_product,max_gross=box_limit(country)
    units=[]
    for idx,it in enumerate(resolved):
        for u in range(it['qty']): units.append({'line':idx,'name':it['catalog_name'],'weight':float(it['weight_kg']),'unit':u+1})
    total=sum(x['weight'] for x in units)
    n=max(1,math.ceil(total/max_product))
    # Greedy LPT with repeated-product spread preference.
    boxes=[{'units':[],'weight':0.0,'counts':Counter()} for _ in range(n)]
    for u in sorted(units,key=lambda x:(-x['weight'], x['name'])):
        eligible=[b for b in boxes if b['weight']+u['weight'] <= max_product+1e-9]
        if not eligible:
            boxes.append({'units':[],'weight':0.0,'counts':Counter()}); n+=1; eligible=boxes
        # First minimize count of same product, then weight.
        b=min(eligible,key=lambda b:(b['counts'][u['name']], b['weight']))
        b['units'].append(u); b['weight']+=u['weight']; b['counts'][u['name']]+=1
    # Local improvement by swapping/moving units. Objective: variance + duplicate imbalance penalty.
    def score(bs):
        ws=[b['weight'] for b in bs]; mean=sum(ws)/len(ws); var=sum((w-mean)**2 for w in ws)
        penalty=0
        names=set(x['name'] for x in units)
        for name in names:
            vals=[b['counts'][name] for b in bs]
            penalty += sum((v-(sum(vals)/len(vals)))**2 for v in vals)*0.03
        return var+penalty
    improved=True; rounds=0
    while improved and rounds<8:
        improved=False; rounds+=1; base=score(boxes)
        for i in range(len(boxes)):
            for j in range(i+1,len(boxes)):
                for a in list(boxes[i]['units']):
                    for b in list(boxes[j]['units']):
                        ni=boxes[i]['weight']-a['weight']+b['weight']; nj=boxes[j]['weight']-b['weight']+a['weight']
                        if ni<=max_product+1e-9 and nj<=max_product+1e-9:
                            boxes[i]['units'].remove(a); boxes[j]['units'].remove(b)
                            boxes[i]['units'].append(b); boxes[j]['units'].append(a)
                            boxes[i]['weight']=ni; boxes[j]['weight']=nj
                            boxes[i]['counts'][a['name']]-=1; boxes[i]['counts'][b['name']]+=1
                            boxes[j]['counts'][b['name']]-=1; boxes[j]['counts'][a['name']]+=1
                            s=score(boxes)
                            if s+1e-9 < base:
                                base=s; improved=True
                            else:
                                boxes[i]['units'].remove(b); boxes[j]['units'].remove(a)
                                boxes[i]['units'].append(a); boxes[j]['units'].append(b)
                                boxes[i]['weight']-=b['weight']; boxes[i]['weight']+=a['weight']
                                boxes[j]['weight']-=a['weight']; boxes[j]['weight']+=b['weight']
                                boxes[i]['counts'][a['name']]+=1; boxes[i]['counts'][b['name']]-=1
                                boxes[j]['counts'][b['name']]+=1; boxes[j]['counts'][a['name']]-=1
    # Clean counts
    for b in boxes:
        b['counts']=Counter(x['name'] for x in b['units'])
        b['units'].sort(key=lambda x:(x['name'],x['unit']))
    return boxes,total

def distribution_status(boxes,max_product):
    if not boxes:
        return 'empty', 0.0
    weights=[float(b.get('weight',0.0)) for b in boxes]
    diff=max(weights)-min(weights)
    if any(w > max_product+1e-9 for w in weights):
        return 'over', diff
    if len(weights)==1 or diff <= 0.50:
        return 'good', diff
    if diff <= 1.00:
        return 'review', diff
    return 'balance', diff

def summarize_box(box):
    grouped=[]
    for name,c in box['counts'].items(): grouped.append((name,c))
    return grouped

def box_type(weight_product):
    if weight_product < 9: return 'CAJA S'
    if weight_product <= 14: return 'CAJA M'
    return 'CAJA L'

# ---------------- PDF generation ----------------
def logo_flowable(width=50*mm):
    from reportlab.platypus import Image
    if LOGO_PATH.exists():
        im=Image(str(LOGO_PATH),width=width,height=width*0.667)
        return im
    return Paragraph('<b>RAÍZ LATINA</b>', getSampleStyleSheet()['Normal'])

def pdf_doc(title='Documento'):
    buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=A4,rightMargin=14*mm,leftMargin=14*mm,topMargin=12*mm,bottomMargin=12*mm,title=title,author='Raíz Latina')
    return buf,doc

class CheckBox(Flowable):
    def __init__(self, size=9):
        Flowable.__init__(self); self.size=size; self.width=size; self.height=size
    def draw(self):
        self.canv.setStrokeColor(colors.black)
        self.canv.setFillColor(colors.white)
        self.canv.setLineWidth(0.8)
        self.canv.rect(0,0,self.size,self.size,stroke=1,fill=1)

def header_table(kind,order,box=None,dispatch=None):
    left=[logo_flowable(48*mm)]
    meta_style=ParagraphStyle('meta_header',fontSize=16,leading=19,alignment=TA_RIGHT,fontName='Helvetica-Bold')
    right=[Paragraph(kind,ParagraphStyle('h',fontSize=17,leading=20,alignment=TA_RIGHT,fontName='Helvetica-Bold')),
           Paragraph(f'<b>Orden #{order}</b>',meta_style),
           Paragraph(box or '',meta_style),
           Paragraph((f'<b>Despacho máximo: {dispatch.strftime("%d/%m/%Y")}</b>' if dispatch else ''), ParagraphStyle('dispatch_header',fontSize=12.5,leading=15,alignment=TA_RIGHT,fontName='Helvetica-Bold'))]
    t=Table([[left,right]],colWidths=[90*mm,90*mm]); t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),4)])); return t

def packing_pdf(order,box_idx,total_boxes,customer,phone,address,country,max_date,box,resolved):
    buf,doc=pdf_doc(f'Packing List caja {box_idx}')
    styles=getSampleStyleSheet(); small=ParagraphStyle('small',parent=styles['Normal'],fontSize=10.5,leading=13)
    label=ParagraphStyle('label',parent=small,fontName='Helvetica-Bold',fontSize=11.5,leading=14)
    story=[header_table('PACKING LIST',order,f'CAJA {box_idx} DE {total_boxes}',max_date),Spacer(1,6)]
    # Datos del destinatario en texto organizado, sin cuadricula.
    story += [Paragraph(f'<b>Cliente:</b> {customer}',small),
              Paragraph(f'<b>Teléfono:</b> {phone}',small),
              Paragraph(f'<b>Dirección:</b> {" · ".join(address)}',small),
              Paragraph(f'<b>País:</b> {country}',small),
              Spacer(1,8)]
    data=[[Paragraph('<b>Producto</b>',small),Paragraph('<b>Cant.</b>',small),Paragraph('<b>Rev 1</b>',small),Paragraph('<b>Rev 2</b>',small)]]
    for name,qty in summarize_box(box): data.append([Paragraph(name,small),str(qty),CheckBox(),CheckBox()])
    pt=Table(data,colWidths=[126*mm,20*mm,18*mm,18*mm],repeatRows=1)
    pt.setStyle(TableStyle([('GRID',(0,0),(-1,-1),0.45,colors.black),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E7E7E7')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(1,1),(-1,-1),'CENTER'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    story += [pt,Spacer(1,10)]
    net=box['weight']; gross=net+1.0; units=len(box['units'])
    # Resumen en texto, sin cuadricula.
    summary=ParagraphStyle('summary',parent=small,fontSize=11.5,leading=15)
    story += [Paragraph(f'<b>Resumen de caja</b>',label),Spacer(1,3),
              Paragraph(f'<b>Peso Neto:</b> {net:.2f} kg',summary),
              Paragraph(f'<b>Peso Bruto:</b> {gross:.2f} kg',summary),
              Paragraph('<b>Peso Real:</b> __________________ kg',summary),
              Paragraph('<b>Dimensiones:</b> Alto ____ × Ancho ____ × Largo ____ cm',summary),
              Paragraph(f'<b>Total unidades:</b> {units}',summary),
              Paragraph(f'<b>Caja recomendada:</b> {box_type(net)}',summary),
              Spacer(1,14),Paragraph('Preparado por: ______________________________________________',small)]
    doc.build(story); return buf.getvalue()

def invoice_pdf(order,customer,phone,address,country,resolved):
    buf,doc=pdf_doc(f'Factura Comercial {order}'); styles=getSampleStyleSheet(); small=ParagraphStyle('small',parent=styles['Normal'],fontSize=8.5,leading=11)
    story=[header_table('FACTURA COMERCIAL',order),Spacer(1,5)]
    story += [Paragraph(f'<b>Cliente:</b> {customer}',small),Paragraph(f'<b>Teléfono:</b> {phone}',small),Paragraph(f'<b>Dirección:</b> {" · ".join(address)}',small),Paragraph(f'<b>País:</b> {country}',small),Spacer(1,8)]
    data=[[Paragraph('<b>Producto</b>',small),Paragraph('<b>Cant.</b>',small),Paragraph('<b>Precio unit. COP</b>',small),Paragraph('<b>Total COP</b>',small)]]
    grand=0
    for x in resolved:
        price=num_value(x['price']); total=price*x['qty']; grand+=total
        data.append([Paragraph(x['catalog_name'],small),str(x['qty']),f'{price:,.0f}',f'{total:,.0f}'])
    data.append(['','',Paragraph('<b>TOTAL</b>',small),Paragraph(f'<b>{grand:,.0f} COP</b>',small)])
    tbl=Table(data,colWidths=[112*mm,18*mm,25*mm,25*mm],repeatRows=1); tbl.setStyle(TableStyle([('GRID',(0,0),(-1,-1),0.4,colors.grey),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E7E7E7')),('ALIGN',(1,1),(-1,-1),'RIGHT'),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('FONTSIZE',(0,0),(-1,-1),8),('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4)])); story += [tbl,Spacer(1,8),Paragraph('Documento generado a partir del catálogo de Tienda Nube. Valores mostrados en COP según el campo Precio del catálogo.',small)]
    doc.build(story); return buf.getvalue()

def distribution_excel(order,resolved,boxes):
    rows=[]
    for bi,b in enumerate(boxes,1):
        counts=Counter(x['name'] for x in b['units'])
        for name,qty in counts.items():
            weight=next(x['weight'] for x in b['units'] if x['name']==name)
            rows.append({'Orden':order,'Caja':bi,'Producto':name,'Cantidad':qty,'Peso unitario kg':weight,'Peso línea kg':qty*weight})
    return pd.DataFrame(rows)

# ---------------- UI ----------------
# Encabezado visual: solo la marca, sin mostrar rutas ni código.
if LOGO_PATH.exists():
    st.image(str(LOGO_PATH), width=260)
    st.markdown('<div style="font-size:26px;font-weight:900;line-height:1;margin-top:-10px;margin-bottom:18px;">MAYORISTAS</div>', unsafe_allow_html=True)
st.title('Distribuidor Logístico')

with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH),width=210)
    st.markdown('### Catálogo Tienda Nube')
    meta=catalog_meta()
    if meta:
        st.success(f"Catálogo vigente: {meta[1]:,} productos")
        st.caption(f"Última actualización: {format_imported_at(meta[0])}")
        if len(meta) > 2 and meta[2] is not None and meta[2] > meta[1]:
            st.caption(f"Históricos conservados: {meta[2]-meta[1]:,}")
    else:
        st.warning('Todavía no hay catálogo cargado.')
    st.markdown('**Límites**')
    st.write('🇸🇻 El Salvador: 9 kg productos + 1 kg embalaje')
    st.write('🌎 Otros países: 14 kg productos + 1 kg embalaje')

# Catalog updater
st.header('1. Actualizar catálogo')
up=st.file_uploader('Sube el CSV original de Tienda Nube, sin modificarlo',type=['csv'],key='catalog')
if st.button('🔄 Actualizar catálogo',type='primary'):
    if not up: st.error('Selecciona primero el CSV original de Tienda Nube.')
    else:
        try:
            df,enc=read_catalog(up); stats=update_catalog(df)
            st.success(f"Catálogo actualizado correctamente. Vigentes: {stats['total']:,} · Nuevos: {stats['new']:,} · Actualizados: {stats['updated']:,} · Sin cambios: {stats['unchanged']:,}.")
            st.info(f"Última actualización: {format_imported_at(stats['version'])} · Históricos conservados: {stats['historical']:,} · Codificación: {enc}")
            st.rerun()
        except Exception as e: st.error(str(e))

st.divider()
st.header('2. Procesar pedido')
pdf=st.file_uploader('Sube el PDF del pedido (ej. 591.pdf)',type=['pdf'],key='order_pdf')
if pdf:
    try:
        order=extract_pdf(pdf.getvalue())
        st.session_state['order_data']=order
        st.success(f"Orden #{order['order_num']} detectada · {len(order['items'])} líneas · {sum(x['qty'] for x in order['items'])} unidades · {order['page_count']} páginas")
        st.dataframe(pd.DataFrame(order['items']),use_container_width=True,hide_index=True)
    except Exception as e: st.error(str(e))

if 'order_data' in st.session_state:
    order=st.session_state['order_data']
    st.subheader('Datos del pedido')
    c1,c2,c3=st.columns(3)
    customer=c1.text_input('Cliente',value=order.get('customer',''))
    phone=c2.text_input('Teléfono',value=order.get('phone',''))
    country=c3.text_input('País',value=order.get('country',''))
    address_default='\n'.join(order.get('address_lines',[]))
    address=st.text_area('Dirección · puedes actualizarla manualmente',value=address_default,height=90)
    max_date=st.date_input('Fecha máxima de despacho',value=date.today(),help='Manual. La fecha de cotización del PDF no se utiliza.')
    address_lines=[x.strip() for x in address.splitlines() if x.strip()]
    cat=catalog_rows()
    if not cat:
        st.warning('Primero debes actualizar el catálogo con el CSV de Tienda Nube.')
    else:
        resolved,issues=match_items(order['items'],cat)
        if issues:
            st.error(f'Hay {len(issues)} producto(s) que deben resolverse antes de generar documentos.')
            for it in issues:
                if it['type']=='missing':
                    st.warning(f"Producto no encontrado: **{it['name']}**. Posiblemente el catálogo de Tienda Nube no está actualizado.")
                    if it['candidates']: st.caption('Sugerencias informativas (NO seleccionadas): '+ ' · '.join(it['candidates']))
                else:
                    st.error(f"Producto ambiguo: **{it['name']}**. Existen varios registros con el mismo nombre normalizado: {', '.join(it['candidates'])}")
        else:
            st.success(f'Validación completa: {len(resolved)}/{len(order["items"])} productos identificados sin usar SKU.')
            boxes,total=pack_items(resolved,country)
            max_product,max_gross=box_limit(country)
            st.subheader('Distribución propuesta')
            status, diff = distribution_status(boxes, max_product)
            if status == 'over':
                st.error(f'⚠️ Hay una caja por encima del límite de {max_product:.0f} kg de productos.')
            elif status == 'balance':
                st.warning(f'⚖️ Hay que equilibrar las cajas: diferencia actual de {diff:.2f} kg entre la más pesada y la más liviana.')
            elif status == 'review':
                st.info(f'ℹ️ Distribución casi equilibrada: diferencia de {diff:.2f} kg entre cajas.')
            else:
                st.success(f'✅ Distribución equilibrada. Diferencia máxima: {diff:.2f} kg.')

            cols=st.columns(len(boxes)) if len(boxes)<=4 else st.columns(4)
            for i,b in enumerate(boxes):
                with cols[i%len(cols)]:
                    st.markdown(f"### 📦 Caja {i+1} de {len(boxes)}")
                    st.metric('Peso productos',f'{b["weight"]:.2f} kg',f'{b["weight"]+1:.2f} kg bruto')
                    st.caption(f'{len(b["units"])} unidades · {box_type(b["weight"])}')
                    for name,qty in sorted(b['counts'].items()):
                        unit_weight = next((float(u['weight']) for u in b['units'] if u['name']==name), 0.0)
                        st.markdown(f"**{name}**  \n{qty} und. · {unit_weight:.2f} kg c/u")
                    if not b['counts']:
                        st.caption('Sin productos')

            total_manual=sum(b['weight'] for b in boxes)
            if any(b['weight']>max_product+1e-9 for b in boxes):
                st.error('La distribución excede el límite. No se pueden generar documentos.')
            else:
                st.info(f"{len(boxes)} caja(s) · {total_manual:.2f} kg netos · límite {max_product:.0f} kg de producto / {max_gross:.0f} kg bruto por caja.")
                if st.button('📄 Generar documentos',type='primary'):
                    files={}
                    files[f'Factura Comercial {order["order_num"]}.pdf']=invoice_pdf(order['order_num'],customer,phone,address_lines,country,resolved)
                    for i,b in enumerate(boxes,1): files[f'Packing List caja {i}.pdf']=packing_pdf(order['order_num'],i,len(boxes),customer,phone,address_lines,country,max_date,b,resolved)
                    xbuf=io.BytesIO(); distribution_excel(order['order_num'],resolved,boxes).to_excel(xbuf,index=False); files[f'Distribucion {order["order_num"]}.xlsx']=xbuf.getvalue()
                    zipbuf=io.BytesIO()
                    with zipfile.ZipFile(zipbuf,'w',zipfile.ZIP_DEFLATED) as z:
                        for name,data in files.items(): z.writestr(f'PEDIDO_{order["order_num"]}/{name}',data)
                    st.session_state['generated']=zipbuf.getvalue()
                    st.success('Documentos generados correctamente.')
                if st.session_state.get('generated'):
                    st.download_button('⬇️ Descargar carpeta del pedido (ZIP)',data=st.session_state['generated'],file_name=f'PEDIDO_{order["order_num"]}.zip',mime='application/zip')
