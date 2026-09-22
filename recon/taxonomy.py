import re,html,json,urllib.request
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
def get(u):
    return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=60).read().decode('utf-8','replace')
def clean(s): return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',s)).split())

h=get('https://www.stats.gov.sa/en/statistics')
DOM={'119021':'Economic Statistics','119025':'Social Statistics','124295':'Environmental and Spatial Statistics'}
# domain -> [subdomain ids]  (from nav hrefs)
dom_subs={}; sub_name={}
for m in re.finditer(r'href="/statistics\?index=(\d+)&(?:amp;)?subindex=(\d+)"[^>]*>(.*?)</a>',h,re.S):
    d,s,n=m.group(1),m.group(2),clean(m.group(3))
    if d in DOM:
        dom_subs.setdefault(d,[]).append(s); sub_name[s]=n
# subdomain -> products
sub_prods={}
for m in re.finditer(r'id="category-collapse-(\d+)"(.*?)(?=<div class=[\'"]accordion-item|$)',h,re.S):
    sid=m.group(1)
    sub_prods[sid]=[(pm.group(1),clean(pm.group(2))) for pm in re.finditer(r'data-category-id="(\d+)"\s*>(.*?)</a>',m.group(2),re.S)]
rows=[]
for d,subs in dom_subs.items():
    for s in dict.fromkeys(subs):
        for cid,pn in sub_prods.get(s,[]):
            rows.append({'domain_id':d,'domain':DOM[d],'subdomain_id':s,'subdomain':sub_name[s],'category_id':cid,'product':pn})
json.dump(rows,open('taxonomy.json','w',encoding='utf-8'),ensure_ascii=False,indent=1)
print('subdomains:',sum(len(dict.fromkeys(v)) for v in dom_subs.values()),'| products:',len(rows),'| unique cat ids:',len(set(r['category_id'] for r in rows)))
cur=None
for r in rows:
    if r['domain']!=cur: cur=r['domain']; print('\n##',cur)
    print(f"   {r['subdomain']:<50} {r['product']}")
