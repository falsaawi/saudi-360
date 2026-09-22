import re,html,json,time,urllib.request,urllib.error,sys
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Saudi360-Inventory/0.1'}
def clean(s): return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',s)).split())
def get(u,tries=4):
    for k in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=90).read().decode('utf-8','replace')
        except urllib.error.HTTPError as e:
            if e.code in (429,503): time.sleep(8*(k+1)); continue
            raise
        except Exception:
            time.sleep(5*(k+1))
    return None

tax=json.load(open('taxonomy.json',encoding='utf-8'))
TAB={'publications':'436312','methodology':'436318','dashboards':'436327'}
res=[]
for i,t in enumerate(tax,1):
    cid=t['category_id']
    url=f"https://www.stats.gov.sa/en/statistics-tabs?tab={TAB['publications']}&category={cid}&delta=500"
    h=get(url)
    if h is None:
        res.append({**t,'error':'fetch_failed'}); print(i,'FAIL',t['product'],flush=True); continue
    # authoritative breadcrumb
    bc=[clean(m.group(1)) for m in re.finditer(r'<li class="breadcrumb-item[^"]*"[^>]*>(.*?)</li>',h,re.S)]
    total=None
    m=re.search(r'Showing \d+ to \d+ of ([\d,]+) entries',h)
    if m: total=int(m.group(1).replace(',',''))
    rels=[]
    for rm in re.finditer(r'data-bs-target="#publication-collapse-(\d+)"(.*?)</a>(.*?)(?=<div class="accordion-item"|</div>\s*</div>\s*</div>\s*<!-- PUBLICATION|$)',h,re.S):
        pid,head,body=rm.group(1),rm.group(2),rm.group(3)
        title=clean(head).replace('Publications','',1).strip()
        files=[(html.unescape(f.group(1)),f.group(2)) for f in re.finditer(r'href="(/documents/[^"]+)"[^>]*>\s*<i class="dl-file-earmark-(\w+)-icon"',body,re.S)]
        rels.append({'pub_id':pid,'title':title,'files':files})
    res.append({**t,'breadcrumb':bc,'total_entries':total,'n_parsed':len(rels),
                'n_files':sum(len(r['files']) for r in rels),
                'ext_mix':{},'releases':rels})
    print(f"{i:>3}/83  {t['product'][:44]:<44} total={total} parsed={len(rels)} files={sum(len(r['files']) for r in rels)}",flush=True)
    time.sleep(1.2)
json.dump(res,open('releases.json','w',encoding='utf-8'),ensure_ascii=False)
print('DONE')
