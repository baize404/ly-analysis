"""離線測試：不連網、不呼叫真的 API。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lyanalysis import collect, laws, lint, site  # noqa: E402
from lyanalysis.analyze import Refusal, analyze  # noqa: E402
from lyanalysis.transcript import build_roster, chunk, ivod_lines, label_speaker, render, speech_lines  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.roster = build_roster(load("legislators.json"))

    def test_roster_aliases(self):
        self.assertEqual(label_speaker("莊委員瑞雄", self.roster), "莊瑞雄（民主進步黨）")
        self.assertEqual(label_speaker("莊瑞雄", self.roster), "莊瑞雄（民主進步黨）")
        self.assertEqual(label_speaker("陳超明", self.roster), "陳超明（無黨籍／中國國民黨黨團）")
        self.assertEqual(label_speaker("鄭委員天財", self.roster), "鄭天財Sra Kacaw（中國國民黨）")
        self.assertEqual(label_speaker("主席", self.roster), "主席")
        self.assertEqual(label_speaker("謝院長龍介", self.roster), "謝龍介（院長，中國國民黨）")
        self.assertEqual(label_speaker("SPEAKER_02", self.roster), "（未辨識講者 3）")
        self.assertEqual(label_speaker("卓院長榮泰", self.roster), "卓院長榮泰")  # 非立委：保留原樣

    def test_out_of_order_segments(self):
        ivod = load("ivod_clip.json")
        ivod["transcript"]["whisperx"].reverse()
        lines = ivod_lines(ivod, self.roster)
        self.assertEqual([l["time"] for l in lines], sorted(l["time"] for l in lines))

    def test_ivod_lines(self):
        lines = ivod_lines(load("ivod_clip.json"), self.roster)
        self.assertEqual(lines[0]["time"], "14:51:06")
        self.assertEqual(lines[0]["speaker"], "莊瑞雄（民主進步黨）")
        self.assertIn("測試第二句 測試第三句", lines[1]["text"])  # 間隔短、同講者 → 合併
        self.assertEqual(lines[-1]["speaker"], "主席")
        self.assertEqual(lines[-1]["time"], "14:54:24")
        self.assertTrue(render(lines).startswith("[14:51:06] 莊瑞雄（民主進步黨）：測試第一句"))

    def test_speech_lines(self):
        speeches = [
            {"順序": 2, "發言者": "謝龍介", "發言者職稱": "委員", "內文": "第二段", "發言時間": "2026-11-20T14:55:00+08:00"},
            {"順序": 1, "發言者": "韓國瑜", "發言者職稱": "主席", "內文": "第一段", "發言時間": "2026-11-20T14:50:00+08:00"},
        ]
        lines = speech_lines(speeches, self.roster)
        self.assertEqual(lines[0]["speaker"], "韓國瑜（主席）")
        self.assertEqual(lines[1]["speaker"], "謝龍介（中國國民黨）")
        self.assertEqual(lines[1]["time"], "14:55")

    def test_chunk(self):
        text = "\n".join(f"第{i}行" + "字" * 20 for i in range(100))
        parts = chunk(text, 500)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual("\n".join(parts), text)


class CollectTest(unittest.TestCase):
    def test_slug_and_group(self):
        self.assertEqual(collect.slug_for("院會-11-6-8", "2026-11-20", "院會"), "2026-11-20-plenary-11-6-8")
        clip = load("ivod_clip.json")
        other = {**clip, "IVOD_ID": 1, "會議資料": {"會議代碼": "委員會-11-6-1-1", "種類": "委員會"}}
        groups = collect.group_ivods([clip, other], ["院會"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["code"], "院會-11-6-8")


class LintTest(unittest.TestCase):
    def test_loaded_terms(self):
        self.assertEqual(lint.check("某委員表示「這是強行表決」。"), [])
        issues = lint.check("國民黨強行表決通過。")
        self.assertTrue(any("強行" in x for x in issues))

    def test_citations(self):
        ok = "依憲法第70條及立法院職權行使法第七十一條之一，以及增修條文第3條。釋字第391號亦有說明。"
        self.assertEqual(lint.check(ok), [])
        bad = lint.check("依刑法第144條，以及釋字第520號。")
        self.assertEqual(len(bad), 2)
        flagged = "依刑法第144條（需查證），以及釋字第520號（需查證）。"
        self.assertEqual(lint.check(flagged), [])

    def test_cn2int(self):
        self.assertEqual(lint.cn2int("七十一"), 71)
        self.assertEqual(lint.cn2int("一百零五"), 105)
        self.assertEqual(lint.cn2int("十"), 10)
        self.assertEqual(lint.cn2int("７０"), 70)


class LawsTest(unittest.TestCase):
    def test_parse_article(self):
        page = ('<div class="col-data"><div class="law-article">'
                '<div class="line-0000">第一項文字。</div>\n<div class="line-0004">一、第一款。</div>'
                '</div>\n</div>')
        self.assertEqual(laws.parse_article(page), "第一項文字。\n一、第一款。")
        self.assertIsNone(laws.parse_article("<html>no article</html>"))

    def test_parse_bulk_and_record(self):
        import io, zipfile
        data = {"UpdateDate": "2026/11/13", "Laws": [{
            "LawName": "測試法", "LawURL": "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=X0000001",
            "LawModifiedDate": "20250101", "LawAbandonNote": "", "LawLevel": "法律",
            "LawArticles": [
                {"ArticleType": "C", "ArticleNo": "", "ArticleContent": "第 一 章 總則"},
                {"ArticleType": "A", "ArticleNo": "第 1 條", "ArticleContent": "第一條內容。"},
                {"ArticleType": "A", "ArticleNo": "第 71-1 條", "ArticleContent": "之一條內容。"}]}]}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ChLaw.json", ("\ufeff" + json.dumps(data, ensure_ascii=False)).encode("utf-8"))
        index, updated = laws.parse_bulk(buf.getvalue())
        self.assertEqual(updated, "2026/11/13")
        rec = laws.law_record(index["測試法"])
        self.assertEqual(rec["pcode"], "X0000001")
        self.assertEqual(rec["articles"], {"1": "第一條內容。", "71-1": "之一條內容。"})
        self.assertFalse(rec["abandoned"])
        self.assertEqual(laws.normalize_no("第 71-1 條"), "71-1")
        self.assertIsNone(laws.normalize_no("第 一 章"))

    def test_laws_page(self):
        cache = {"fetched_at": "2026-11-21T17:00:00+08:00", "database_updated": "2026/11/13", "laws": {
            "中華民國憲法": {"pcode": "A0000001", "url": "u", "modified": "19470101",
                             "articles": {"70": {"text": "測試<條文>", "source": "openapi"}}}}}
        original = laws.load_cache
        laws.load_cache = lambda: cache
        try:
            html_text = site.build_laws({"site_title": "測試站"})
        finally:
            laws.load_cache = original
        self.assertIn("測試&lt;條文&gt;", html_text)
        self.assertIn("pcode=A0000001&amp;flno=70", html_text)
        self.assertIn("（原文未取得）", html_text)  # 快取裡沒有的條文

    def test_reference_text_without_cache(self):
        text = laws.reference_text({"fetched_at": None, "articles": {}})
        self.assertIn("中華民國憲法 第 70 條", text)
        self.assertIn("原文未取得", text)
        self.assertIn("第 71 條之 1", text)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, refuse_models=()):
        self.calls = []
        self.refuse_models = set(refuse_models)
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        if kwargs["model"] in self.refuse_models:
            msg = SimpleNamespace(content=[], stop_reason="refusal", usage=usage,
                                  stop_details={"category": "test", "explanation": "測試婉拒"})
        else:
            text = f"## 一句話摘要\n第 {len(self.calls)} 次呼叫"
            msg = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="…"),
                                           SimpleNamespace(type="text", text=text)],
                                  stop_reason="end_turn", usage=usage)
        return FakeStream(msg)


CFG = {"model": "main-model", "fallback_model": "backup-model", "chunk_chars": 1000,
       "chunk_max_tokens": 100, "final_max_tokens": 100, "review_pass": True}
META = {"title": "測試會議", "code": "院會-11-6-8", "kind": "院會", "date": "2026-11-20",
        "source": "ai_full", "source_label": "AI 逐字稿"}


class AnalyzeTest(unittest.TestCase):
    def test_single_chunk_with_review(self):
        client = FakeClient()
        text, record = analyze(META, "[09:00:00] 主席：開會", CFG, client)
        self.assertEqual(len(client.calls), 2)  # 撰寫 + 審稿
        self.assertIn("第 2 次呼叫", text)
        self.assertEqual(record["model"], "main-model")
        self.assertEqual(record["usage"]["calls"], 2)
        system = client.calls[0]["system"][0]["text"]
        self.assertIn("法條參考", system)

    def test_multi_chunk(self):
        client = FakeClient()
        transcript = "\n".join("[09:00:00] 主席：" + "字" * 300 for _ in range(10))
        analyze(META, transcript, CFG, client)
        self.assertGreater(len(client.calls), 3)

    def test_refusal_fallback(self):
        client = FakeClient(refuse_models={"main-model"})
        text, record = analyze(META, "[09:00:00] 主席：開會", CFG, client)
        self.assertEqual(record["model"], "backup-model")
        self.assertIn("測試婉拒", record["refusal"])

    def test_refusal_without_fallback(self):
        client = FakeClient(refuse_models={"main-model", "backup-model"})
        with self.assertRaises(Refusal):
            analyze(META, "[09:00:00] 主席：開會", CFG, client)


class SiteTest(unittest.TestCase):
    def test_build_escapes_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, out = Path(tmp) / "meetings", Path(tmp) / "docs"
            folder = data / "2026-11-20-plenary-11-6-8"
            folder.mkdir(parents=True)
            meta = {**META, "slug": folder.name, "name": "測試", "ivods": [
                {"id": 1, "kind": "Full", "url": "https://ivod.ly.gov.tw/Play/Full/1M/1", "start": "2026-11-20T09:00:00+08:00"}],
                "analysis": {"generated_at": "2026-11-21T10:00:00+08:00", "model": "m", "lint": ["測試提示"]}}
            (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            (folder / "analysis.md").write_text("## 標題\n\n<script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
                                                encoding="utf-8")
            (folder / "transcript.txt").write_text("[09:00:00] 主席：開會\n", encoding="utf-8")
            cfg = {"site_title": "測試站", "repo_url": "https://example.com"}
            self.assertEqual(site.build(data, out, cfg), 1)
            page = (out / "m" / folder.name / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", page)
            self.assertIn("&lt;script&gt;", page)
            self.assertIn('<div class="table-wrap"><table>', page)
            self.assertIn("測試提示", page)
            self.assertTrue((out / "m" / folder.name / "transcript.txt").exists())
            self.assertIn(folder.name, (out / "index.html").read_text(encoding="utf-8"))


class NeedsAnalysisTest(unittest.TestCase):
    def test_rules(self):
        import run
        cfg = {"reanalyze_growth_ratio": 1.25}
        self.assertFalse(run.needs_analysis({"source": "none"}, cfg))
        self.assertTrue(run.needs_analysis({"source": "ai_full", "source_rank": 2}, cfg))
        done = {"source_rank": 2, "transcript_sha256": "a", "transcript_chars": 1000}
        self.assertFalse(run.needs_analysis({"source": "ai_full", "source_rank": 2, "transcript_sha256": "b",
                                             "transcript_chars": 1100, "analysis": done}, cfg))
        self.assertTrue(run.needs_analysis({"source": "ai_full", "source_rank": 2, "transcript_sha256": "b",
                                            "transcript_chars": 1300, "analysis": done}, cfg))
        self.assertTrue(run.needs_analysis({"source": "gazette", "source_rank": 3, "transcript_sha256": "a",
                                            "transcript_chars": 1000, "analysis": done}, cfg))


if __name__ == "__main__":
    unittest.main()
