"""把 data/meetings 的分析結果輸出成靜態網站（docs/，供 GitHub Pages 使用）。"""
from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown as md

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Taipei")
KIND_LABEL = {"院會": "院會", "委員會": "委員會", "黨團協商": "黨團協商", "全院委員會": "全院委員會", "公聽會": "公聽會"}

DISCLAIMER = (
    "本頁由 AI（Claude）依立法院公開資料自動產生，未經人工審核，可能有錯誤或遺漏。"
    "內容僅供理解會議脈絡，不代表任何政黨立場，也不構成法律意見。"
    "請以原始影片、立法院公報與全國法規資料庫為準。"
)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


NESTED_ITEM = re.compile(r"^( +)([-*+] |\d+\. )", re.M)


def render_markdown(text: str) -> str:
    # AI 產生的內容一律不允許夾帶 HTML：先把 < 轉成實體，再轉 Markdown。
    safe = text.replace("&", "&amp;").replace("<", "&lt;")
    # Python-Markdown 的巢狀清單需縮排 4 格；AI 常用 2 格，先統一調整。
    safe = NESTED_ITEM.sub(lambda m: (m.group(1) * 2 if len(m.group(1)) % 4 == 2 else m.group(1)) + m.group(2), safe)
    out = md.markdown(safe, extensions=["tables", "sane_lists", "fenced_code"], output_format="html")
    return out.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")


def page(title: str, body: str, root: str, site_title: str, description: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description or site_title)}">
<link rel="stylesheet" href="{root}style.css">
</head>
<body>
<header class="site-header"><div class="wrap">
  <a class="brand" href="{root}">{esc(site_title)}</a>
  <nav><a href="{root}">會議列表</a><a href="{root}laws.html">法條參考</a><a href="{root}about.html">方法與限制</a></nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="site-footer"><div class="wrap">
  <p>資料來源：立法院議事轉播（IVOD）、立法院公報，經 <a href="https://v2.ly.govapi.tw/">LYAPI</a>（OpenFun，CC BY 4.0）取得；法條取自<a href="https://law.moj.gov.tw/">全國法規資料庫</a>。</p>
  <p>分析由 AI 產生，可能有誤；本站不隸屬任何政黨或政府機關。</p>
</div></footer>
</body>
</html>
"""


def status_of(meta: dict) -> tuple[str, str]:
    if meta.get("analysis"):
        if meta["analysis"].get("lint"):
            return "warn", "已分析・有待確認項目"
        return "ok", "已分析"
    if meta.get("analysis_error"):
        return "error", "分析失敗"
    if meta.get("source", "none") == "none":
        return "wait", "等待逐字稿"
    return "wait", "等待分析"


def load_meetings(data_dir: Path) -> list[dict]:
    metas = []
    for path in sorted(data_dir.glob("*/meta.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["_dir"] = path.parent
        metas.append(meta)
    metas.sort(key=lambda m: (m.get("date") or "", m.get("ivods", [{}])[0].get("start") or "" if m.get("ivods") else ""),
               reverse=True)
    return metas


def _meeting_card(meta: dict) -> str:
    cls, label = status_of(meta)
    name = meta.get("name") or meta.get("reason") or ""
    if len(name) > 120:
        name = name[:120] + "…"
    return f"""<li class="card">
  <a href="m/{esc(meta['slug'])}/">
    <div class="card-top"><span class="date">{esc(meta.get('date'))}</span>
      <span class="tag">{esc(KIND_LABEL.get(meta.get('kind'), meta.get('kind')))}</span>
      <span class="status {cls}">{esc(label)}</span></div>
    <h2>{esc(meta.get('title'))}</h2>
    <p class="muted">{esc(name)}</p>
    <p class="source">文字來源：{esc(meta.get('source_label'))}</p>
  </a>
</li>"""


def build_index(metas: list[dict], cfg: dict) -> str:
    intro = """<section class="intro">
<p>本站每天自動整理立法院院會、全院委員會與黨團協商的內容：依官方影片的 AI 逐字稿（公報刊出後改用公報）撰寫客觀摘要，並列各方主張、數字查核清單與相關法條。</p>
<p class="muted">""" + esc(DISCLAIMER) + """</p>
</section>"""
    if not metas:
        listing = '<p class="empty">目前還沒有會議資料。排程第一次執行後，這裡會列出最近的會議。</p>'
    else:
        listing = '<ul class="cards">' + "\n".join(_meeting_card(m) for m in metas) + "</ul>"
    updated = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    body = f"{intro}\n{listing}\n<p class=\"muted small\">網站更新時間：{updated}（臺北時間）</p>"
    return page(cfg["site_title"], body, "", cfg["site_title"])


def _links(meta: dict) -> str:
    items = []
    for iv in meta.get("ivods") or []:
        if iv.get("kind") == "Full" and iv.get("url"):
            items.append(f'<a href="{esc(iv["url"])}">完整會議影片（IVOD {esc(iv.get("id"))}）</a>')
    clips = [iv for iv in meta.get("ivods") or [] if iv.get("kind") == "Clip"]
    if clips:
        items.append(f"委員發言片段 {len(clips)} 段（見下方清單）")
    if meta.get("ppg_url"):
        items.append(f'<a href="{esc(meta["ppg_url"])}">議事暨公報資訊網</a>')
    if (meta["_dir"] / "transcript.txt").exists():
        items.append('<a href="transcript.txt">本頁使用的逐字稿（純文字）</a>')
    items.append(f'<a href="https://v2.ly.govapi.tw/meet/{esc(meta["code"])}">LYAPI 會議資料</a>')
    return "<ul class=\"links\">" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


def _clips(meta: dict) -> str:
    clips = [iv for iv in meta.get("ivods") or [] if iv.get("kind") == "Clip"]
    if not clips:
        return ""
    rows = "".join(
        f'<li><a href="{esc(iv.get("url"))}">{esc(iv.get("speaker"))}</a> '
        f'<span class="muted">{esc((iv.get("start") or "")[11:19])}</span></li>' for iv in clips)
    return f'<details class="clips"><summary>委員發言片段（{len(clips)}）</summary><ul>{rows}</ul></details>'


def build_meeting(meta: dict, cfg: dict) -> str:
    cls, label = status_of(meta)
    analysis = meta.get("analysis") or {}
    head = f"""<p class="crumbs"><a href="../../">會議列表</a> › {esc(meta.get('date'))}</p>
<h1>{esc(meta.get('title'))}</h1>
<p class="meta-line"><span class="tag">{esc(KIND_LABEL.get(meta.get('kind'), meta.get('kind')))}</span>
<span>{esc(meta.get('date'))}</span><span class="status {cls}">{esc(label)}</span></p>
<p class="muted">{esc(meta.get('name') or meta.get('reason') or '')}</p>
<aside class="notice"><strong>閱讀前請注意：</strong>{esc(DISCLAIMER)}
<br>文字來源：{esc(meta.get('source_label'))}。</aside>"""

    if analysis.get("lint"):
        items = "".join(f"<li>{esc(x)}</li>" for x in analysis["lint"])
        head += f"""<aside class="notice warn"><strong>程式檢查提示：</strong>以下項目可能需要人工確認（機械檢查，可能誤判）：<ul>{items}</ul></aside>"""
    if analysis.get("refusal"):
        head += f"""<aside class="notice"><strong>備註：</strong>主要模型婉拒了這次分析（{esc(analysis['refusal'])}），本頁改由 {esc(analysis.get('model'))} 產生。</aside>"""

    analysis_path = meta["_dir"] / "analysis.md"
    if analysis and analysis_path.exists():
        content = render_markdown(analysis_path.read_text(encoding="utf-8"))
        foot = (f"<p class=\"muted small\">分析產生時間：{esc(analysis.get('generated_at'))}，"
                f"模型：{esc(analysis.get('model'))}，依據：{esc(analysis.get('source_label') or meta.get('source_label'))}。</p>")
        if analysis.get("mode") == "manual":
            foot += ("<p class=\"muted small\">本篇依本站同一套撰寫規則在 Claude 對話中完成後匯入，"
                     "經過相同的程式檢查；逐字稿改版（例如公報刊出）時，自動流程會重新分析。</p>")
        body = f'<article class="analysis">{content}</article>{foot}'
    elif meta.get("analysis_error"):
        err = meta["analysis_error"]
        body = f'<p class="empty">這場會議的分析失敗，將於下次排程重試。<br><span class="muted">原因：{esc(err.get("explain") or err.get("message"))}</span></p>'
    elif meta.get("source", "none") == "none":
        body = '<p class="empty">影片的 AI 逐字稿尚未產生，下次排程會再檢查。</p>'
    else:
        body = '<p class="empty">逐字稿已取得，等待下次排程分析。</p>'

    side = f"<section class=\"sources\"><h2>原始資料</h2>{_links(meta)}{_clips(meta)}</section>"
    return page(f"{meta.get('date')} {meta.get('title')}｜{cfg['site_title']}", head + body + side, "../../",
                cfg["site_title"], description=(meta.get("name") or "")[:150])


def build_about(cfg: dict) -> str:
    body = """<h1>方法與限制</h1>
<h2>資料從哪裡來</h2>
<ul>
<li><strong>立法院公報發言紀錄</strong>：官方逐字紀錄，最可靠，但通常會議後數週才刊出。刊出後本站會自動以公報重新分析。</li>
<li><strong>完整會議影片的 AI 逐字稿</strong>：由 LYAPI 以語音辨識產生，會議當天或隔天即可取得，但可能有同音錯字，講者標示也可能錯置。</li>
<li><strong>委員發言片段的 AI 逐字稿</strong>：沒有完整影片逐字稿時才使用，可能缺少主席宣告與表決過程。</li>
<li><strong>法條</strong>：每週自動向全國法規資料庫抓取指定條文，分析時只允許引用這些條文；引用其他條文必須標註「需查證」。</li>
</ul>
<h2>如何維持客觀</h2>
<ol>
<li><strong>寫作規則</strong>：只能根據提供的資料；不使用帶褒貶的用語（除非是加上引號的發言者原話）；不評價對錯、不預測選舉；事實附時間點，數字註明是誰說的；有爭議的法律問題並列各方見解，不自行裁定。</li>
<li><strong>固定格式</strong>：每篇依相同段落撰寫；事實查核表的狀態只能是「資料內可確認」「待查證」「發言間互相矛盾」。</li>
<li><strong>中立性審稿</strong>：初稿完成後，再由 AI 以審稿人身分檢查用語、各方篇幅與依據，直接修訂。</li>
<li><strong>程式檢查</strong>：不靠 AI 的機械檢查，掃描引號外的立場用語，以及未標「需查證」的法條引用，結果直接顯示在該頁。</li>
<li><strong>可對照</strong>：每頁附原始影片、議事網與逐字稿連結。AI 產生的內容一律以純文字顯示。</li>
</ol>
<h2>限制</h2>
<ul>
<li>全程沒有人工審核。AI 可能誤解發言、漏掉重點，或在摘要時失去語氣與脈絡。</li>
<li>「待查證」的數字本站不會自行查證，請參考建議的官方來源。</li>
<li>本站不構成法律意見。法律問題請以全國法規資料庫、司法院與專業意見為準。</li>
</ul>
<h2>原始碼</h2>
<p>程式、提示詞與所有分析紀錄都公開在 <a href=\"""" + esc(cfg["repo_url"]) + """\">GitHub</a>，歡迎檢視與回報問題。</p>"""
    return page(f"方法與限制｜{cfg['site_title']}", body, "", cfg["site_title"])


def build_laws(cfg: dict) -> str:
    from . import laws as lawmod

    law_cfg = lawmod.load_config()
    cache = lawmod.load_cache()
    stamp = cache.get("database_updated") or (cache.get("fetched_at") or "")[:10]
    head = """<h1>法條參考</h1>
<p>本站分析時，AI 只能引用下列條文；引用其他條文時必須標註「需查證」，並由程式檢查。
條文由程式自<a href="https://law.moj.gov.tw/">全國法規資料庫</a>開放資料下載，未經改寫。"""
    if stamp:
        head += f"資料版本：{esc(stamp)}；本站下載時間：{esc((cache.get('fetched_at') or '')[:16].replace('T', ' '))}。"
    head += "</p>"
    if not cache.get("laws"):
        return page(f"法條參考｜{cfg['site_title']}",
                    head + '<p class="empty">條文尚未下載，下次排程會自動更新。</p>', "", cfg["site_title"])

    toc, sections = [], []
    for i, law in enumerate(law_cfg["laws"]):
        info = cache["laws"].get(law["name"], {})
        anchor = f"law-{i}"
        toc.append(f'<li><a href="#{anchor}">{esc(law["name"])}</a></li>')
        meta = []
        if info.get("modified"):
            meta.append(f"最新異動日期 {esc(info['modified'])}")
        if info.get("abandoned"):
            meta.append("<strong>注意：資料庫標示此法規已廢止</strong>")
        if info.get("url"):
            meta.append(f'<a href="{esc(info["url"])}">全國法規資料庫全文</a>')
        arts = []
        for no in law["articles"]:
            item = info.get("articles", {}).get(no)
            link = lawmod.article_url(info.get("pcode"), no)
            label = esc(lawmod.article_label(no))
            label_html = f'<a href="{esc(link)}">{label}</a>' if link else label
            text = esc(item["text"]).replace("\n", "<br>") if item else '<span class="muted">（原文未取得）</span>'
            note = law.get("article_notes", {}).get(no)
            note_html = f'<p class="article-note">本站註：{esc(note)}</p>' if note else ""
            arts.append(f'<div class="article"><div class="article-no">{label_html}</div><div class="article-text">{text}{note_html}</div></div>')
        sections.append(f"""<section class="law" id="{anchor}">
<h2>{esc(law['name'])}</h2>
<p class="muted small">收錄原因：{esc(law.get('reason') or '')}</p>
<p class="small">{'｜'.join(meta)}</p>
{''.join(arts)}
</section>""")
    note = """<aside class="notice">大法官解釋不在全國法規資料庫的開放資料中。本站僅收錄釋字第 264、391 號的摘要（本站自行改寫），
供 AI 理解背景，原文請見<a href="https://cons.judicial.gov.tw/">憲法法庭網站</a>。</aside>"""
    body = head + note + '<nav class="toc"><ul>' + "".join(toc) + "</ul></nav>" + "".join(sections)
    return page(f"法條參考｜{cfg['site_title']}", body, "", cfg["site_title"])


def build(data_dir: Path, out_dir: Path, cfg: dict) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    meetings_out = out_dir / "m"
    if meetings_out.exists():
        shutil.rmtree(meetings_out)
    shutil.copyfile(ROOT / "site" / "style.css", out_dir / "style.css")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    metas = load_meetings(data_dir)
    for meta in metas:
        folder = meetings_out / meta["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(build_meeting(meta, cfg), encoding="utf-8")
        transcript = meta["_dir"] / "transcript.txt"
        if transcript.exists():
            shutil.copyfile(transcript, folder / "transcript.txt")
    (out_dir / "index.html").write_text(build_index(metas, cfg), encoding="utf-8")
    (out_dir / "about.html").write_text(build_about(cfg), encoding="utf-8")
    (out_dir / "laws.html").write_text(build_laws(cfg), encoding="utf-8")
    return len(metas)
