import re,html,json,time,urllib.request,urllib.error
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
def clean(s): return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',s)).split())
def get(u,tries=4):
    for k in range(tries):
        try: return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=90).read().decode('utf-8','replace')
        except urllib.error.HTTPError as e:
            if e.code in (429,503): time.sleep(8*(k+1)); continue
            return None
        except Exception: time.sleep(4*(k+1))
    return None
def parse(h):
    out=[]
    for b in re.split(r'<!-- PUBLICATION ACCORDION ITEM -->',h):
        m=re.search(r'data-bs-target="#publication-collapse-(\d+)"(.*?)</a>',b,re.S)
        if not m: continue
        title=clean(m.group(2))
        title=re.sub(r'^>?\s*Publications\s*','',title).lstrip('> ').strip()
        meta=dict(re.findall(r'<span class="th">\s*(\w+)\s*</span>\s*<span class="td">\s*([^<]*?)\s*</span>',b))
        files=[{'url':html.unescape(f[0]),'ext':f[1]} for f in
               re.findall(r'href="(/documents/[^"]+)"[^>]*>\s*<i class="dl-file-earmark-(\w+)-icon"',b,re.S)]
        out.append({'pub_id':m.group(1),'title':title,'year':meta.get('Year'),
                    'periodicity':meta.get('Periodicity'),'month':meta.get('Month'),'files':files})
    return out

tax=json.load(open('taxonomy.json',encoding='utf-8'))
res=[]
for i,t in enumerate(tax,1):
    cid=t['category_id']; seen={}; claimed=None; bc=None
    for s in range(1,7):
        h=get(f'https://www.stats.gov.sa/en/statistics-tabs?tab=436312&category={cid}&delta=60&start={s}')
        if h is None: break
        if bc is None:
            bc=[clean(x) for x in re.findall(r'<li class="breadcrumb-item[^"]*"[^>]*>(.*?)</li>',h,re.S)]
            m=re.search(r'Showing \d+ to \d+ of ([\d,]+) entries',h)
            claimed=int(m.group(1).replace(',','')) if m else None
        rows=parse(h)
        if not rows: break
        for r in rows: seen[r['pub_id']]=r
        time.sleep(0.9)
    rel=list(seen.values())
    res.append({**t,'breadcrumb':bc,'claimed_total':claimed,'retrieved':len(rel),'releases':rel})
    print(f"{i:>3}/83 {t['product'][:42]:<42} claimed={str(claimed):<5} got={len(rel):<4} files={sum(len(r['files']) for r in rel)}",flush=True)
json.dump(res,open('inventory.json','w',encoding='utf-8'),ensure_ascii=False)
print('DONE')
