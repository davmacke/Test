"""Small, rate-limited public-page collector. No login, proxy or challenge bypass."""
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET
from store import stamp

UA='PropertyScout/1.0 (personal property research; low-frequency public-page checks)'
PORTALS={'rightmove.co.uk','zoopla.co.uk','onthemarket.com'}

def canonical(url):
    p=urlsplit(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):raise ValueError('Only public HTTPS listing URLs are supported')
    return urlunsplit(('https',p.netloc.lower(),p.path or '/','',''))

def host_match(host,allowed):return any(host==h or host.endswith('.'+h) for h in allowed)

def public_url(url,allowed):
    url=canonical(url); host=urlsplit(url).hostname
    if not host_match(host,allowed):raise ValueError('URL is outside configured estate-agent domains')
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError('Private network addresses are not allowed')
    return url

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args):return None

class Fetcher:
    def __init__(self,allowed,delay=2):
        self.allowed=allowed; self.delay=delay; self.last={};self.rules={};self.opener=build_opener(NoRedirect)
    def raw(self,url,cap=2500000):
        for _ in range(4):
            url=public_url(url,self.allowed);host=urlsplit(url).hostname
            time.sleep(max(0,self.delay-(time.monotonic()-self.last.get(host,0))))
            self.last[host]=time.monotonic()
            try:
                with self.opener.open(Request(url,headers={'User-Agent':UA,'Accept':'text/html, application/xml, text/plain'}),timeout=15) as r:
                    body=r.read(cap+1)
                    if len(body)>cap:raise ValueError('Page exceeds collector size limit')
                    encoding=r.headers.get_content_charset()
                    if not encoding:
                        charset=re.search(br'charset\s*=\s*[\"\x27]?([a-zA-Z0-9_-]+)',body[:5000])
                        encoding=charset[1].decode('ascii') if charset else 'utf-8'
                    try:decoded=body.decode(encoding)
                    except (UnicodeDecodeError,LookupError):decoded=body.decode('windows-1252',errors='replace')
                    return decoded,r.headers.get('Content-Type','')
            except HTTPError as ex:
                if ex.code in (301,302,303,307,308):url=urljoin(url,ex.headers['Location']);continue
                raise
        raise ValueError('Too many redirects')
    def robots(self,url):
        origin='https://'+urlsplit(url).netloc
        if origin not in self.rules:
            rp=RobotFileParser(origin+'/robots.txt')
            try:text,_=self.raw(origin+'/robots.txt',500000);rp.parse(text.splitlines())
            except HTTPError as ex:
                if ex.code in (404,410):rp.parse(['User-agent: *','Allow: /'])
                else:raise ValueError(f'robots.txt unavailable (HTTP {ex.code}); source held')
            self.rules[origin]=rp
        return self.rules[origin]
    def get(self,url):
        rp=self.robots(url)
        # Conservative: also honour explicit GPTBot restrictions for this AI-built collector.
        if not rp.can_fetch('PropertyScout',url) or not rp.can_fetch('GPTBot',url):raise ValueError('Blocked by site crawl rules')
        self.delay=max(self.delay,rp.crawl_delay('PropertyScout') or rp.crawl_delay('*') or 0)
        return self.raw(url)[0]

class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.headings=[];self.links=[];self.ld=[];self.meta={};self.skip=0;self.script=None;self.h=0;self.heading=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='script':self.script=[] if a.get('type','').lower()=='application/ld+json' else None;self.skip+=1
        if tag in ('style','nav','footer','noscript'):self.skip+=1
        if tag in ('h1','h2'):self.h+=1;self.heading=[]
        if tag=='meta':self.meta[a.get('property',a.get('name','')).lower()]=a.get('content','')
        if tag=='a' and a.get('href'):self.links.append(a['href'])
        if tag in ('p','div','br','li','section','h1','h2','tr'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag=='script':
            if self.script is not None:
                try:self.ld.append(json.loads(''.join(self.script)))
                except (ValueError,TypeError):pass
            self.script=None;self.skip=max(0,self.skip-1)
        if tag in ('style','nav','footer','noscript'):self.skip=max(0,self.skip-1)
        if tag in ('h1','h2'):
            if self.heading:self.headings.append(' '.join(self.heading))
            self.h=max(0,self.h-1)
        if tag in ('p','div','li','section','h1','h2','tr'):self.parts.append('\n')
    def handle_data(self,data):
        if self.script is not None:self.script.append(data)
        if self.h and not self.skip:self.heading.append(data.strip())
        if not self.skip:self.parts.append(data)
    @property
    def text(self):return '\n'.join(' '.join(x.split()) for x in ''.join(self.parts).splitlines() if x.strip())

def money(x):
    try:return float(str(x).replace(',','').replace('£','').strip())
    except (ValueError,TypeError):return None

def structures(items):
    for obj in items:
        if isinstance(obj,list):yield from structures(obj)
        elif isinstance(obj,dict):
            yield obj
            if '@graph' in obj:yield from structures(obj['@graph'])
            if isinstance(obj.get('mainEntity'),dict):yield obj['mainEntity']

def parse_listing(html,url,source):
    page=Page();page.feed(html);text=page.text
    # Remove related listings from the text used for price/rent/status extraction.
    core=re.split(r'(?im)^(?:similar properties|other properties|you may also like|properties you might like)\b',text)[0]
    title=page.meta.get('og:title') or (page.headings[0] if page.headings else '')
    address_heading=next((h for h in page.headings if ',' in h and len(h)>15),None)
    prop=None
    for obj in structures(page.ld):
        types=obj.get('@type',[]);types=[types] if isinstance(types,str) else types
        if any(t in ('House','SingleFamilyResidence','Residence','Apartment','RealEstateListing','Product') for t in types):prop=obj;break
    prop=prop or {};desc=prop.get('description') or page.meta.get('description') or page.meta.get('og:description','')
    if not title and not desc:return None
    evidence_text=title+'\n'+str(desc)+'\n'+core
    if not re.search(r'(?i)(?:for sale|buy\b|asking price|offers (?:over|in|in excess)|guide price)',evidence_text):return None
    if re.search(r'(?i)\b(?:for rent|to let|to rent)\b',title) and not re.search('(?i)for sale',title):return None
    offers=prop.get('offers',{});offers=offers[0] if isinstance(offers,list) and len(offers)==1 else offers
    price=money(offers.get('price')) if isinstance(offers,dict) and offers.get('priceCurrency','GBP')=='GBP' else None
    if price is None:price=money(page.meta.get('product:price:amount'))
    if price is None:
        hit=re.search(r'£\s*([\d,]+)(?:\.\d{2})?',title+'\n'+core[:4500]);price=money(hit[1]) if hit else None
    if price is not None and price<20000:price=None # never treat monthly rent/fees as house price
    beds=prop.get('numberOfBedrooms')
    if isinstance(beds,dict):beds=beds.get('value')
    try:beds=int(beds) if beds is not None else None
    except (ValueError,TypeError):beds=None
    if beds is None:
        hit=re.search(r'(?i)\b([2-9])\s*(?:-| )?(?:bedrooms?|beds?)\b',title+' '+str(desc))
        beds=int(hit[1]) if hit else None
        if beds is None:
            word=re.search(r'(?i)\b(two|three)\s*[- ]\s*(?:bedrooms?|beds?)\b',title+' '+str(desc))
            if word:beds={'two':2,'three':3}[word[1].lower()]
    current=re.search(r'(?i)(?:currently (?:let|achieving|paying)|current(?:ly)? (?:rental (?:income|payment)|rent)|rent(?:al income)?(?: of)?|paying)[^£\n]{0,65}£\s*([\d,]+)(?:\.\d+)?\s*(pcm|per calendar month|per month|a month|pa|per annum|a year|/year)',core)
    rent=None;rent_basis='Missing - needs rent evidence'
    if current:
        rent=money(current[1]);rent=rent/12 if current[2].lower() in ('pa','per annum','a year','/year') else rent
        rent_basis='Advertised rent - verify basis'
    if rent is None:
        hit=re.search(r'(?i)annual (?:gross |rental )?(?:income|rent)[^£\n]{0,40}£\s*([\d,]+)',core)
        if hit:rent=money(hit[1])/12;rent_basis='Advertised annual rent - verify basis'
    if rent is None:
        hit=re.search(r'(?i)(?:marketed to let|estimated (?:rent|rental)|potential (?:rent|rental)|rental (?:estimate|potential))[^£\n]{0,70}£\s*([\d,]+)\s*(?:pcm|per month|per calendar month)',str(desc)+'\n'+core)
        if hit:rent=money(hit[1]);rent_basis='Agent asking/estimated rent - not tenancy'
    listed='listed' if re.search(r'(?i)\bgrade\s*(?:ii|2|i)\s*\*?\s*listed|\blisted building\b',core) and not re.search(r'(?i)(?:not (?:a )?listed|listed (?:property|building)\s*[:—-]?\s*no)',core) else 'unknown'
    status='unknown'
    if re.search(r'(?i)\b(?:sold stc|sold subject to contract|sale agreed|under offer)\b',core[:5000]):status='sstc'
    elif re.search(r'(?i)\b(?:no longer on the market|withdrawn)\b',core[:5000]):status='withdrawn'
    elif re.search(r'(?im)^\s*sold\s*$',core[:5000]):status='sold'
    elif price is not None:status='advertised'
    refurb=bool(re.search(r'(?i)(?:requires?|in need of|scope for|benefit from|needs)[^.\n]{0,75}(?:refurb|modernis|renovat|cosmetic|updat)',core))
    country=source.get('country')
    tenure='freehold' if re.search(r'(?i)\bfreehold\b',core) else ('leasehold' if re.search(r'(?i)\bleasehold\b',core) else 'unknown')
    return dict(url=canonical(url),name=(address_heading or title)[:200],beds=beds,price=price,rent=rent,rent_basis=rent_basis,
                country=country,tenure=tenure,route='refurb' if refurb else 'unknown',
                annual_charges=0 if tenure=='freehold' else None,listed=listed,sale_status=status,
                auction=bool(re.search(r'(?i)\b(?:for sale by auction|modern method of auction|reservation fee|unconditional auction)\b',core)),
                cash_only=bool(re.search(r'(?i)cash (?:buyers? only|purchase only|offers only)',core)),
                observed_at=stamp(),source=source['name'],extraction='Automatically extracted - unverified',
                fact_snippet=(str(desc)+' '+core[:1200])[:1800],page_hash=hashlib.sha256(html.encode()).hexdigest())

def discover(fetch,source,max_urls=2000):
    out=set(source.get('seed_urls',[]));queue=list(source.get('discovery_urls',[]));visited=set();errors=[]
    pattern=re.compile(source['listing_pattern'])
    while queue and len(visited)<5 and len(out)<max_urls:
        url=queue.pop(0)
        if url in visited:continue
        visited.add(url)
        try:text=fetch.get(url)
        except Exception as ex:errors.append(f'{url}: {type(ex).__name__}: {ex}');continue
        try:
            root=ET.fromstring(text)
            links=[x.text for x in root.iter() if x.tag.split('}')[-1]=='loc' and x.text]
            if root.tag.split('}')[-1]=='sitemapindex':queue.extend(links[:5]);continue
        except ET.ParseError:
            page=Page();page.feed(text);links=page.links
        for link in links:
            try:u=canonical(urljoin(url,link))
            except ValueError:continue
            if host_match(urlsplit(u).hostname,source['domains']) and pattern.search(urlsplit(u).path):out.add(u)
            if len(out)>=max_urls:break
    return sorted(out),errors

def scan(store,sources,limit=20):
    run=store.start_scan();successful=failed=found=0
    try:
        for source in sources:
            if not source.get('enabled'):
                store.event(source['name'],'disabled',source.get('reason','Not enabled'));continue
            fetch=Fetcher(source['domains'],source.get('delay',2))
            try:
                urls,errors=discover(fetch,source)
                for error in errors:failed+=1;store.event(source['name'],'discovery_failed',error)
                checked=store.checked_urls()
                # Rotate ALL inspected pages, including over-budget ones, to avoid repeatedly
                # inspecting the same expensive homes and never reaching cheap listings.
                urls.sort(key=lambda u:(u in checked,checked.get(u,'')))
                for url in urls[:limit]:
                    try:
                        html=fetch.get(url);p=parse_listing(html,url,source)
                        if p and p['beds'] in (2,3) and (p['price'] is None or p['price']<=100000):store.upsert(p);found+=1
                        successful+=1
                    except Exception as ex:
                        failed+=1;store.event(source['name'],'page_failed',f'{url}: {type(ex).__name__}: {ex}')
                    finally:store.mark_checked(url)
                store.event(source['name'],'ok',f'{len(urls)} discovered; up to {limit} checked. Failed pages never update last-observed dates.')
            except Exception as ex:
                failed+=1;store.event(source['name'],'source_failed',f'{type(ex).__name__}: {ex}')
        status='completed' if failed==0 else ('partial' if successful else 'failed')
        store.finish_scan(run,status,f'{found} property observations; {successful} pages fetched; {failed} failures')
    except Exception as ex:
        store.finish_scan(run,'failed',f'{type(ex).__name__}: {ex}');raise
    return dict(observations=found,pages=successful,failures=failed)
