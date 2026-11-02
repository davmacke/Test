"""Single-process property dashboard and durable, bounded scheduled scans."""
import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from finance import analyse, settings
from store import Store, stamp
from collector import scan, canonical

ROOT=Path(__file__).parent

class Application:
    def __init__(self,data_dir=None):
        self.store=Store(Path(data_dir or os.getenv('DATA_DIR',ROOT/'data'))/'scout.sqlite3')
        self.sources=json.loads((ROOT/'sources.json').read_text(encoding='utf-8'))
        self.lock=threading.Lock();self.stop=threading.Event()
        if not self.store.config('seed_imported',False):
            for p in json.loads((ROOT/'seed.json').read_text(encoding='utf-8')):self.store.upsert(p,at=p['observed_at'])
            self.store.set_config('seed_imported',True)
        # Interrupted scans are explicit, not silently left as live/running.
        with self.store.connect() as db:db.execute("UPDATE scans SET status='interrupted',finished=?,detail='Process restarted before completion' WHERE status='running'",(stamp(),))
    def state(self):
        c=settings(self.store.config('settings',{}));items=[]
        for p in self.store.listings():
            p['id']=hashlib.sha256(p['url'].encode()).hexdigest()[:16]
            p['analysis']=analyse(p,c);items.append(p)
        items.sort(key=lambda p:({'verified':0,'model_fits':1,'needs_data':2,'excluded':3}[p['analysis']['status']],-p['analysis']['priority']))
        return dict(listings=items,settings=c,scans=self.store.scans(),source_runs=self.store.source_runs(),changes=self.store.changes(),
                    sources=[dict(name=s['name'],enabled=s['enabled'],reason=s.get('reason','')) for s in self.sources],
                    scanning=self.lock.locked(),schedule_hours=float(os.getenv('SCAN_INTERVAL_HOURS','12')),
                    automatic=os.getenv('AUTO_SCAN','false').lower()=='true',now=stamp())
    def launch_scan(self):
        if not self.lock.acquire(blocking=False):return False
        def work():
            try:scan(self.store,self.sources,limit=min(30,max(1,int(os.getenv('PAGES_PER_SOURCE','20')))))
            finally:self.lock.release()
        threading.Thread(target=work,daemon=True).start();return True
    def scheduler(self):
        while not self.stop.wait(30):
            interval=max(3600,float(os.getenv('SCAN_INTERVAL_HOURS','12'))*3600)
            runs=self.store.scans();last=runs[0]['started'] if runs else None
            from datetime import datetime,timezone
            elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds() if last else interval
            if elapsed>=interval:self.launch_scan()

def handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version='PropertyScout'
        def log_message(self,fmt,*args):pass # no credentials or property notes in access logs
        def reply(self,status,payload,kind='application/json'):
            raw=json.dumps(payload,allow_nan=False).encode() if kind=='application/json' else payload
            self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(raw)
        def authenticated(self):
            password=os.getenv('SCOUT_PASSWORD','')
            if not password:
                host=urlsplit('http://'+self.headers.get('Host','')).hostname
                return self.client_address[0] in ('127.0.0.1','::1') and host in ('localhost','127.0.0.1','::1')
            try:
                prefix,token=self.headers.get('Authorization','').split(' ',1)
                user,provided=base64.b64decode(token,validate=True).decode().split(':',1)
                return prefix=='Basic' and hmac.compare_digest(user.encode(),b'owner') and hmac.compare_digest(provided.encode(),password.encode())
            except (ValueError,UnicodeError):return False
        def gate(self):
            if self.authenticated():return True
            self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="Property Scout", charset="UTF-8"');self.end_headers();return False
        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/healthz':return self.reply(200,dict(status='ok'))
            if not self.gate():return
            if path=='/api/state':return self.reply(200,app.state())
            assets={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
            if path in assets:
                file,kind=assets[path];return self.reply(200,(ROOT/'static'/file).read_bytes(),kind)
            self.reply(404,dict(error='Not found'))
        def do_POST(self):
            if not self.gate():return
            origin=self.headers.get('Origin');host=self.headers.get('Host')
            if self.headers.get('X-Scout-Request')!='1' or (origin and urlsplit(origin).netloc!=host):return self.reply(403,dict(error='Same-origin request required'))
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=30000:raise ValueError('Request too large or empty')
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):raise ValueError('Expected a JSON object')
                path=urlsplit(self.path).path
                if path=='/api/scan':
                    return self.reply(202 if app.launch_scan() else 409,dict(message='Scan queued' if app.lock.locked() else 'Already running'))
                if path=='/api/settings':
                    c=settings(body);app.store.set_config('settings',c);return self.reply(200,c)
                if path=='/api/review':
                    url=canonical(body['url']);p=app.store.listing(url)
                    if not p:raise ValueError('Unknown listing')
                    edits=body.get('edits',{});allowed={'price','rent','works','vacant_months','annual_charges'}
                    for k,v in edits.items():
                        if k in allowed:
                            import math
                            if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or v<0:raise ValueError('Enter a nonnegative finite number')
                        elif k=='listed' and v in ('unknown','unlisted','listed'):pass
                        elif k=='mortgage' and v in ('pending','confirmed','rejected'):pass
                        elif k=='route' and v in ('unknown','ready','refurb'):pass
                        elif k=='sale_status' and v in ('unknown','available','sstc','sold','withdrawn'):pass
                        elif k=='tenure' and v in ('unknown','freehold','leasehold'):pass
                        else:raise ValueError('Unsupported review field')
                        p[k]=v
                    evidence=body.get('evidence',{})
                    if not isinstance(evidence,dict) or any(k not in ('rent','works','costs','listed','mortgage','title','lease') or not isinstance(v,str) or len(v)>1200 for k,v in evidence.items()):raise ValueError('Invalid evidence note')
                    p['evidence']=evidence;p['reviewed_at']=stamp()
                    # Reviews do not refresh advert observation dates.
                    app.store.upsert(p);return self.reply(200,dict(saved=True))
                self.reply(404,dict(error='Not found'))
            except (ValueError,KeyError,TypeError,json.JSONDecodeError) as ex:self.reply(400,dict(error=str(ex)))
    return Handler

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--scan-once',action='store_true');args=parser.parse_args()
    app=Application()
    if args.scan_once:
        print(json.dumps(scan(app.store,app.sources)));return
    if os.getenv('AUTO_SCAN','false').lower()=='true':threading.Thread(target=app.scheduler,daemon=True).start()
    port=int(os.getenv('PORT','8765'));bind=os.getenv('BIND_HOST','0.0.0.0' if os.getenv('PORT') else '127.0.0.1')
    if bind=='0.0.0.0' and not os.getenv('SCOUT_PASSWORD'):raise SystemExit('Set SCOUT_PASSWORD before exposing the dashboard')
    server=ThreadingHTTPServer((bind,port),handler(app))
    print(f'Property Scout listening on port {port}; automatic scans: {os.getenv("AUTO_SCAN","false")}')
    try:server.serve_forever()
    finally:app.stop.set();server.server_close()

if __name__=='__main__':main()
