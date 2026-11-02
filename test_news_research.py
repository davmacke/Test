import ast
import json
import sqlite3
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
import news_research as n
NOW=datetime(2026,11,2,19,tzinfo=timezone.utc)
def article(**kw):
    return dict(url="https://www.reuters.com/world/test",title="Iran diplomacy and war risks remain",seendate="20261102T180000Z",**kw)
class Response:
    def __init__(self,body):self.body=body
    def raise_for_status(self):pass
    def json(self):return self.body
class NewsTests(unittest.TestCase):
    def test_source_time_and_both_scenarios_are_mentions_only(self):
        a=n.normalize(article(),"Energy and geopolitics",NOW)
        self.assertEqual(a["first_seen_at"],NOW.isoformat())
        self.assertEqual(a["publication_at"],None)
        self.assertIn("Diplomacy mentioned",a["tags"])
        self.assertIn("Conflict/disruption mentioned",a["tags"])
        self.assertEqual(n.available_at(a),NOW)
    def test_unknown_source_bad_url_and_future_news_rejected(self):
        for changes in ({"url":"https://reuters.com.evil.example/test"},{"url":"javascript:alert(1)"},
                        {"url":"https://secret@reuters.com/test"},{"seendate":"20261103T180000Z"},
                        {"seendate":"20261020T180000Z"},{"seendate":None}):
            raw=article();raw.update(changes);self.assertIsNone(n.normalize(raw,"Macro",NOW))
    def test_first_seen_not_retroactively_backdated_and_titles_versioned(self):
        c=sqlite3.connect(":memory:")
        try:
            get=lambda *args,**kw:Response({"articles":[article()]})
            r=n.collect(c,get,NOW)
            self.assertEqual(r["new_articles"],1)
            later=n.collect(c,get,NOW+timedelta(minutes=20))
            self.assertEqual(later["new_articles"],0)
            self.assertEqual(later["articles"][0]["first_seen_at"],NOW.isoformat())
            changed=article();changed["title"]="Iran talks progress reportedly"
            n.collect(c,lambda *args,**kw:Response({"articles":[changed]}),NOW+timedelta(minutes=30))
            self.assertEqual(c.execute("SELECT count(*) FROM articles").fetchone()[0],2)
        finally:c.close()
    def test_failure_and_empty_feed_are_not_bullish_evidence(self):
        c=sqlite3.connect(":memory:")
        try:
            def fail(*a,**k):raise ValueError("bad response")
            r=n.collect(c,fail,NOW);self.assertEqual(r["status"],"NO_CURRENT_NEWS")
            self.assertTrue(all(v["status"]=="FAILED" for v in r["sources"].values()))
            r=n.collect(c,lambda *a,**k:Response({"articles":[]}),NOW)
            self.assertEqual(r["status"],"NO_CURRENT_NEWS");self.assertEqual(r["execution_mode"],"RESEARCH_ONLY")
        finally:c.close()
    def test_repeated_domains_not_confirmation_and_no_order_methods(self):
        for file in ("news_runner.py","news_research.py"):
            tree=ast.parse(Path(__file__).with_name(file).read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Attribute):self.assertNotIn(node.attr,("place_demo_order","close_demo_position","cancel_demo_order"))
    def test_staged_replay_equal_budget_costs_cash_and_determinism(self):
        start=datetime(2025,1,1,tzinfo=timezone.utc)
        bars=[{"fromDate":(start+timedelta(days=i)).isoformat(),"open":10000.,"high":10100.,"low":9900.,"close":10000.} for i in range(400)]
        r=n.staged_replay(bars,NOW)
        self.assertEqual(r["status"],"COMPLETE");self.assertEqual(len(r["results"]),9)
        for item in r["results"]:
            self.assertEqual(item["initial_gbp"],1250)
            self.assertLess(item["ending_gbp"],1250)
            self.assertGreaterEqual(item["cash_gbp"],0)
            self.assertLessEqual(item["deployed_gbp"],1250)
            if item["strategy"]=="Five weekly installments":self.assertEqual(item["purchases"],5)
            elif item["strategy"]=="Conditional recovery additions":self.assertEqual(item["purchases"],1)
        # A later close cannot change an earlier purchase schedule.
        changed=[dict(b) for b in bars];changed[-1].update(close=10050.)
        other=n.staged_replay(changed,NOW)
        self.assertEqual([x["purchases"] for x in r["results"]],[x["purchases"] for x in other["results"]])
    def test_insufficient_history_is_explicit(self):
        self.assertEqual(n.staged_replay([],NOW)["status"],"NEED_MORE_HISTORY")
    def test_publisher_rss_fallback_preserves_publication_and_first_seen(self):
        xml="<rss><channel><item><title>Oil falls as Iran talks progress</title><link>https://www.wsj.com/world/iran-test</link><pubDate>Mon, 02 Nov 2026 18:00:00 GMT</pubDate></item></channel></rss>"
        class RSSResponse:
            text=xml
            def raise_for_status(self):pass
        def get(url,**kwargs):
            if url==n.ENDPOINT:raise ValueError("Search provider unavailable")
            return RSSResponse()
        c=sqlite3.connect(":memory:")
        try:
            report=n.collect(c,get,NOW)
            self.assertEqual(report["status"],"TRACKING")
            self.assertEqual(report["new_articles"],1)
            a=report["articles"][0]
            self.assertEqual(a["provider_time_kind"],"publisher_rss_publication")
            self.assertEqual(a["publication_at"],"2026-11-02T18:00:00+00:00")
            self.assertEqual(a["first_seen_at"],NOW.isoformat())
            self.assertEqual(n.available_at(a),NOW)
            self.assertEqual(a["topic"],"Energy and geopolitics")
        finally:c.close()
    def test_xml_entities_are_rejected(self):
        with self.assertRaises(ValueError):n.parse_rss('<!DOCTYPE rss [<!ENTITY test "bad">]><rss/>')
if __name__=="__main__":unittest.main()
