"""Timestamped news discovery and equal-budget staged-entry research; no order methods."""
import hashlib
import json
import math
import re
from datetime import datetime,timedelta,timezone
from statistics import mean
from urllib.parse import urlsplit,urlunsplit
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from bollinger_rsi import completed_daily_candles
from data_quality import utc_timestamp

ENDPOINT="https://api.gdeltproject.org/api/v2/doc/doc"
QUERIES={
 "Energy and geopolitics":'("Brent crude" OR "Strait of Hormuz" OR "Shell plc" OR "Iran war") sourcelang:english',
 "Technology":'("technology stocks" OR Nvidia OR "AI spending") sourcelang:english',
 "Macro":'("Federal Reserve" OR "Bank of England" OR "trade tariffs") sourcelang:english'}
DOMAINS=("reuters.com","apnews.com","bbc.com","bbc.co.uk","cnbc.com","ft.com","bloomberg.com","theguardian.com",
         "euronext.com","shell.com","bp.com","eia.gov","opec.org","ukmto.org","federalreserve.gov","bankofengland.co.uk","wsj.com")
EVENTS={
 "Diplomacy mentioned":r"\b(ceasefire|diploma\w*|negotiat\w*|peace|reopen\w*|talks)\b",
 "Conflict/disruption mentioned":r"\b(attack\w*|strike\w*|missile\w*|blockade\w*|disrupt\w*|war|sanction\w*)\b",
 "Results/forecast mentioned":r"\b(earnings|profit\w*|guidance|forecast\w*|outlook)\b",
 "Uncertainty/attribution mentioned":r"\b(rumou?r\w*|reportedly|sources|could|may|might|expect\w*|hope\w*)\b"}
def normalize(article,topic,now):
    url=str(article.get("url",""))
    parts=urlsplit(url);domain=(parts.hostname or "").lower()
    if parts.scheme not in ("https","http") or parts.username or parts.password:return None
    if not any(domain==d or domain.endswith("."+d) for d in DOMAINS):return None
    title=re.sub(r"\s+"," ",str(article.get("title",""))).strip()[:300]
    stamp=utc_timestamp(article.get("seendate"))
    if not title or stamp is None or stamp>now+timedelta(minutes=5) or now-stamp>timedelta(hours=48):return None
    canonical=urlunsplit((parts.scheme,parts.netloc,parts.path,parts.query,""))
    return {"id":hashlib.sha256((canonical+"|"+title).encode()).hexdigest(),
            "url":canonical,"domain":domain,"title":title,"topic":topic,
            "provider_seen_at":stamp.isoformat(),"first_seen_at":now.isoformat(),
            "tags":[name for name,pattern in EVENTS.items() if re.search(pattern,title,re.I)],
            "verification":"Headline mention only; event truth/direction not independently verified",
            "publication_at":article.get("publication_at"),
            "provider_time_kind":article.get("provider_time_kind","gdelt_discovery")}

def available_at(article):
    # Backtests must not treat provider timestamps as the bot's earlier knowledge.
    return max(utc_timestamp(article["first_seen_at"]),utc_timestamp(article["provider_seen_at"]))

def initialize(c):
    c.execute("""CREATE TABLE IF NOT EXISTS articles(
      id TEXT PRIMARY KEY,url TEXT,domain TEXT,title TEXT,topic TEXT,provider_seen_at TEXT,
      first_seen_at TEXT,last_seen_at TEXT,payload TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS fetches(stamp TEXT PRIMARY KEY,payload TEXT)")
    c.commit()

RSS_FEEDS = {"Dow Jones markets RSS":"https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
             "Dow Jones world RSS":"https://feeds.content.dowjones.io/public/rss/RSSWorldNews"}

def parse_rss(text):
    if len(text)>2000000 or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():raise ValueError("Invalid or oversized XML")
    root=ET.fromstring(text);output=[]
    for item in root.findall(".//item")[:150]:
        try:
            stamp=parsedate_to_datetime(item.findtext("pubDate") or "")
            if stamp.tzinfo is None:continue
        except (ValueError,TypeError,OverflowError):continue
        output.append({"url":item.findtext("link"),"title":item.findtext("title"),"seendate":stamp.isoformat(),
                       "publication_at":stamp.isoformat(),"provider_time_kind":"publisher_rss_publication"})
    return output

def save(c,a,now):
    old=c.execute("SELECT first_seen_at,payload FROM articles WHERE id=?",(a["id"],)).fetchone()
    if old:
        original=json.loads(old[1]);a["first_seen_at"]=old[0]
        for key in ("provider_seen_at","topic","publication_at","provider_time_kind"):
            if key in original:a[key]=original[key]
    c.execute("INSERT OR REPLACE INTO articles VALUES(?,?,?,?,?,?,?,?,?)",
      (a["id"],a["url"],a["domain"],a["title"],a["topic"],a["provider_seen_at"],a["first_seen_at"],now.isoformat(),json.dumps(a)))
    return int(old is None)

def error_info(error):
    return {"status":"FAILED","error":type(error).__name__,"http_status":getattr(getattr(error,"response",None),"status_code",None)}

def collect(c,get,now):
    initialize(c);outcomes={};new=0
    for topic,query in QUERIES.items():
        try:
            response=get(ENDPOINT,params={"query":query,"mode":"artlist","format":"json","maxrecords":100,
                           "sort":"datedesc","timespan":"24h"},timeout=20)
            response.raise_for_status();body=response.json()
            articles=body.get("articles",[])
            if not isinstance(articles,list):raise ValueError("Invalid article list")
            accepted=0
            for raw in articles:
                a=normalize(raw,topic,now)
                if a is None:continue
                new+=save(c,a,now)
                accepted+=1
            outcomes[topic]={"status":"OK" if accepted else "NO_MATCHES","received":len(articles),"accepted":accepted}
        except Exception as error:outcomes[topic]=error_info(error)
    if not any(v["status"]=="OK" for v in outcomes.values()):
        for name,url in RSS_FEEDS.items():
            try:
                response=get(url,timeout=15);response.raise_for_status();items=parse_rss(response.text);accepted=0
                for raw in items:
                    title=str(raw.get("title",""))
                    topic=("Energy and geopolitics" if re.search(r"\b(oil|brent|iran|hormuz|shell|opec|ceasefire)\b",title,re.I) else
                           "Technology" if re.search(r"\b(tech\w*|nvidia|artificial intelligence|AI|semiconductor\w*)\b",title,re.I) else "Macro")
                    a=normalize(raw,topic,now)
                    if a is not None:new+=save(c,a,now);accepted+=1
                outcomes[name]={"status":"OK" if accepted else "NO_MATCHES","received":len(items),"accepted":accepted}
            except Exception as error:outcomes[name]=error_info(error)
    c.execute("INSERT OR REPLACE INTO fetches VALUES(?,?)",(now.isoformat(),json.dumps(outcomes)))
    c.commit()
    rows=[json.loads(r[0]) for r in c.execute("SELECT payload FROM articles ORDER BY provider_seen_at DESC LIMIT 150")]
    recent=[r for r in rows if now-available_at(r)<=timedelta(hours=24)]
    tags={tag:sum(tag in r["tags"] for r in recent) for tag in EVENTS}
    # Keep diplomatic and escalation evidence visible together; counts are not probabilities.
    energy=[r for r in recent if r["topic"]=="Energy and geopolitics"]
    mixed=any("Diplomacy mentioned" in r["tags"] for r in energy) and any("Conflict/disruption mentioned" in r["tags"] for r in energy)
    status="TRACKING" if any(v["status"]=="OK" for v in outcomes.values()) else "NO_CURRENT_NEWS"
    return {"status":status,"fetched_at":now.isoformat(),"new_articles":new,"sources":outcomes,"articles":rows,
            "topic_counts":{topic:sum(r["topic"]==topic for r in recent) for topic in QUERIES},
            "mention_counts":tags,"energy_scenario":"BOTH_SCENARIOS_MENTIONED" if mixed else "MANUAL_REVIEW_REQUIRED",
            "execution_mode":"RESEARCH_ONLY",
            "limitations":["GDELT discovery is delayed/incomplete, not a low-latency trading wire.",
              "GDELT discovery time differs from publication time; publisher RSS dates are labeled publication. Bot first-seen is retained; revised titles create new records.",
              "Distinct domains may repeat one wire report; counts are not independent confirmations.",
              "Keyword tags describe mentions, including negations and quotations; they do not verify facts or estimate direction/probability.",
              "Rumours do not override trade approval. Archived first-seen records are needed for a later point-in-time news replay."]}

def staged_replay(candles,now,budget=1250.,entry_cost=.006,exit_cost=.001):
    bars=completed_daily_candles(candles,now)
    results=[]
    for offset in (252,189,126):
        start=len(bars)-offset
        if start<55:continue
        end=min(start+126,len(bars));window=bars[start:end]
        if len(window)<126:continue
        for strategy in ("Buy and hold","Five weekly installments","Conditional recovery additions"):
            cash=budget;units=0.;trades=0;last_buy=None;last_day=None;peak=budget;drawdown=0.;curve=[]
            for i in range(start,end):
                b=bars[i];day=utc_timestamp(b["fromDate"]);opening=b["open"]/100.;close=b["close"]/100.
                buy=i==start
                if trades<5 and last_day is not None:
                    if strategy=="Five weekly installments":buy=(day-last_day).days>=7
                    elif strategy=="Conditional recovery additions":
                        previous=bars[i-1]["close"]/100.
                        avg=mean(x["close"]/100. for x in bars[i-20:i])
                        buy=i-start>=5 and (day-last_day).days>=7 and previous>avg and previous>bars[i-2]["close"]/100. and opening<last_buy
                if buy and (strategy!="Buy and hold" or trades==0):
                    tranche=budget if strategy=="Buy and hold" else budget/5
                    amount=min(cash,tranche)
                    if amount>.01:
                        units+=amount/(opening*(1+entry_cost));cash-=amount
                        trades+=1;last_buy=opening;last_day=day
                value=cash+units*close*(1-exit_cost)
                peak=max(peak,value);drawdown=min(drawdown,value/peak-1)
                curve.append({"date":b["fromDate"][:10],"equity_gbp":round(value,2)})
            results.append({"strategy":strategy,"start":window[0]["fromDate"][:10],"end":window[-1]["fromDate"][:10],
              "initial_gbp":budget,"ending_gbp":round(value,2),"net_return_pct":round(100*(value/budget-1),3),
              "max_drawdown_pct":round(100*drawdown,3),"purchases":trades,"cash_gbp":round(cash,2),
              "deployed_gbp":round(budget-cash,2),"curve":curve})
    return {"status":"COMPLETE" if results else "NEED_MORE_HISTORY","symbol":"SHEL.L","results":results,
            "assumptions":["Equal £1,250 budgets available from the start, unleveraged Shell share prices converted from GBX to GBP.",
             "126-session overlapping historical windows; retrospective comparisons, not independent validation or a profit forecast.",
             "Buy and hold invests at first open. Fixed schedule invests 20% at first open and four later opens at least seven calendar days apart.",
             "Conditional adds need seven calendar days since purchase, prior close above its 20-session average and above the prior close, and current open below last entry. Five tranches maximum.",
             "Decisions use prior completed bars and buy at the following open. No future close is used to choose an entry.",
             "Assumed all-in entry friction 0.6%, exit friction 0.1%; not a reconstruction of eToro charges. Price-only; dividends, cash interest and financing excluded.",
             "No stops in these long-horizon research comparisons; existing demo stops and holding deadlines are unchanged.",
             "No historical news was reconstructed. This is a price-based staged-entry comparison, not a backtest of buying rumours."]}
