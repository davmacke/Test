import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

def stamp(): return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self,path):
        self.path=str(path); Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS listings (url TEXT PRIMARY KEY,payload TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY,url TEXT,at TEXT,kind TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY,started TEXT,finished TEXT,status TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS source_runs(id INTEGER PRIMARY KEY,at TEXT,source TEXT,status TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS url_checks(url TEXT PRIMARY KEY,checked TEXT);
            ''')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback();raise
        finally:db.close()
    def config(self,key,default):
        with self.connect() as db:r=db.execute('SELECT value FROM config WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else default
    def set_config(self,key,value):
        with self.connect() as db:db.execute('INSERT INTO config VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))
    def listing(self,url):
        with self.connect() as db:r=db.execute('SELECT payload FROM listings WHERE url=?',(url,)).fetchone()
        return json.loads(r[0]) if r else None
    def listings(self):
        with self.connect() as db:rows=db.execute('SELECT payload,first_seen,last_seen FROM listings').fetchall()
        return [json.loads(r[0]) | dict(first_seen=r[1],last_seen=r[2]) for r in rows]
    def upsert(self,p,at=None):
        at=at or stamp(); old=self.listing(p['url'])
        # Preserve human confirmations separately from freshly extracted advert facts.
        if old:
            for k in ('evidence','works','vacant_months','mortgage'):
                if k not in p and k in old:p[k]=old[k]
            for k in ('price','rent','beds','annual_charges','listed','sale_status','route','country','tenure'):
                if old.get(k)!=p.get(k):
                    p.setdefault('evidence',{}).pop({'annual_charges':'costs','price':'mortgage','beds':'title','sale_status':'availability'}.get(k,k),None)
        with self.connect() as db:
            if old:
                for k in ('price','rent','sale_status','listed'):
                    if old.get(k)!=p.get(k):db.execute('INSERT INTO changes(url,at,kind,detail) VALUES(?,?,?,?)',(p['url'],at,k,json.dumps(dict(before=old.get(k),after=p.get(k)))))
            else:db.execute('INSERT INTO changes(url,at,kind,detail) VALUES(?,?,?,?)',(p['url'],at,'new','New research lead'))
            db.execute('INSERT INTO listings VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen',(p['url'],json.dumps(p),at,at))
    def event(self,source,status,detail):
        with self.connect() as db:db.execute('INSERT INTO source_runs(at,source,status,detail) VALUES(?,?,?,?)',(stamp(),source,status,detail[:1000]))
    def source_runs(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM source_runs ORDER BY id DESC LIMIT 50')]
    def changes(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM changes ORDER BY id DESC LIMIT 100')]
    def scans(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 20')]
    def checked_urls(self):
        with self.connect() as db:return dict(db.execute('SELECT url,checked FROM url_checks').fetchall())
    def mark_checked(self,url):
        with self.connect() as db:db.execute('INSERT INTO url_checks VALUES(?,?) ON CONFLICT(url) DO UPDATE SET checked=excluded.checked',(url,stamp()))
    def start_scan(self):
        with self.connect() as db:
            cur=db.execute('INSERT INTO scans(started,status) VALUES(?,?)',(stamp(),'running')); return cur.lastrowid
    def finish_scan(self,scan,status,detail):
        with self.connect() as db:db.execute('UPDATE scans SET finished=?,status=?,detail=? WHERE id=?',(stamp(),status,detail,scan))
