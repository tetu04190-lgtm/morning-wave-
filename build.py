#!/usr/bin/env python3
"""
MORNING WAVE — 朝のニュースラジオを毎朝自動で焼く

RSS収集 → 本文取得 → Claude APIで台本 → edge-ttsで音声合成 → mp3 + Podcast RSS

環境変数:
  ANTHROPIC_API_KEY  任意。無ければ見出しをそのまま読む短い番組になる。
  SITE_URL           GitHub Pages の公開URL（例 https://user.github.io/morning-wave）
"""

import asyncio, datetime, html, json, os, re, subprocess, sys
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import edge_tts, feedparser, requests, trafilatura

# ── 設定 ─────────────────────────────────────────────
JST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime.now(JST)

SITE = os.environ.get("SITE_URL", "").rstrip("/")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

VOICE = "ja-JP-NanamiNeural"   # 男性は ja-JP-KeitaNeural
SPEED = "+4%"

TARGET_MIN = 40                # 番組の目標尺（分）
CHARS_PER_MIN = 370            # この声・速度での実測目安。長さがずれたらここを直す
KEEP_DAYS = 14

UA = {"User-Agent": "Mozilla/5.0 (compatible; MorningWave/1.0)"}
DOCS = Path(__file__).resolve().parent / "docs"
EPISODES = DOCS / "episodes"


def gnews(q: str) -> str:
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(q)}+when:1d&hl=ja&gl=JP&ceid=JP:ja"


SEGMENTS = [
    dict(name="きょうの主なニュース", take=6,
         feeds=["https://www.nhk.or.jp/rss/news/cat0.xml",
                "https://www.nhk.or.jp/rss/news/cat1.xml"]),
    dict(name="AIの話題", take=6,
         feeds=["https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
                gnews("生成AI OR ChatGPT OR Claude"),
                gnews("AI 企業 導入")]),
    dict(name="iPhone・Mac・Windows", take=6,
         feeds=["https://iphone-mania.jp/feed/",
                "https://pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf",
                "https://forest.watch.impress.co.jp/data/rss/1.0/wf/feed.rdf"]),
    dict(name="IT・ガジェット", take=6,
         feeds=["https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
                "https://gigazine.net/news/rss_2.0/",
                "https://k-tai.watch.impress.co.jp/data/rss/1.0/ktw/feed.rdf"]),
    dict(name="ビジネスと経済", take=6,
         feeds=["https://www.nhk.or.jp/rss/news/cat5.xml",
                gnews("決算 OR 業績 企業")]),
    dict(name="千葉・印西のニュース", take=4,
         feeds=[gnews("印西市 OR 千葉ニュータウン"),
                gnews("千葉県 OR 成田市 OR 白井市")]),
]


# ── 収集 ─────────────────────────────────────────────
def tidy(s: str) -> str:
    s = re.sub(r"<[^>]*>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def split_source(title: str):
    m = re.match(r"^(.+?)\s+-\s+([^-]{2,24})$", title)
    return (m.group(1), m.group(2)) if m else (title, "")


def collect(seg) -> list[dict]:
    lists = []
    for url in seg["feeds"]:
        try:
            d = feedparser.parse(url)
            arts = []
            for e in d.entries[:10]:
                t, src = split_source(tidy(e.get("title", "")))
                if not t:
                    continue
                arts.append(dict(title=t, link=e.get("link", ""),
                                 desc=tidy(e.get("summary", ""))[:200],
                                 source=src or tidy(d.feed.get("title", "")),
                                 body=""))
            lists.append(arts)
        except Exception as ex:
            print(f"  ! {url}: {ex}", file=sys.stderr)

    out, seen = [], set()
    for i in range(10):
        for l in lists:
            if i >= len(l) or len(out) >= seg["take"]:
                continue
            a = l[i]
            k = a["title"][:20]
            if k in seen:
                continue
            seen.add(k)
            out.append(a)
    return out[: seg["take"]]


def fetch_body(a: dict):
    """記事本文を取ってくる。取れなければ要約文のまま。"""
    url = a["link"]
    if not url or "news.google.com" in url:
        return                       # Googleニュースは中間ページなので本文が無い
    try:
        r = requests.get(url, headers=UA, timeout=12)
        r.encoding = r.apparent_encoding
        text = trafilatura.extract(r.text, include_comments=False) or ""
        a["body"] = re.sub(r"\s+", " ", text).strip()[:1600]
    except Exception:
        pass


# ── 台本 ─────────────────────────────────────────────
def write_script(seg, items, per: int) -> dict:
    if not API_KEY:
        return dict(intro=f"続いては、{seg['name']}です。",
                    lines=[f"{a['title']}。{(a['body'] or a['desc'])[:200]}" for a in items])

    listing = "\n\n".join(
        f"【記事{i+1}】{a['title']}\n{(a['body'] or a['desc'])[:1400]}"
        for i, a in enumerate(items))

    prompt = f"""あなたは朝のラジオニュース番組のパーソナリティです。
コーナー「{seg['name']}」の原稿を書いてください。

{listing}

要件:
- intro: このコーナーへの導入。1文、30字以内。
- lines: 各記事の原稿。**1本あたり{per}字前後**。記事は{len(items)}本、linesも必ず{len(items)}件。
- 見出しの言い換えで終わらせない。何が起きたのか、なぜ今それが話題なのか、聞き手の生活や仕事にどう関わるのかまで話す。
- 話し言葉で、耳で聞いて分かる長さの文をつなぐ。数字と固有名詞は正確に残す。
- **与えられた記事本文に書かれていない事実、数字、人名を足さない。** 分からないことは触れない。
- 「〜とのことです」「〜だそうです」の連発を避ける。
- 記号、絵文字、括弧、箇条書きは使わない。声に出して読める文だけ。

JSONのみを返す。前置きもコードブロックも不要:
{{"intro":"...","lines":["...","..."]}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=dict(model="claude-sonnet-5", max_tokens=8000,
                      messages=[dict(role="user", content=prompt)]),
            timeout=180,
        )
        r.raise_for_status()
        text = "".join(c["text"] for c in r.json()["content"] if c["type"] == "text")
        j = json.loads(text[text.index("{"): text.rindex("}") + 1])
        if j.get("lines"):
            return j
    except Exception as ex:
        print(f"  ! 台本生成をスキップ: {ex}", file=sys.stderr)

    return dict(intro=f"続いては、{seg['name']}です。",
                lines=[f"{a['title']}。{(a['body'] or a['desc'])[:200]}" for a in items])


# ── 音声 ─────────────────────────────────────────────
async def synth(text: str, out: Path):
    await edge_tts.Communicate(text, VOICE, rate=SPEED).save(str(out))


def duration(mp3: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(mp3)], capture_output=True, text=True, check=True)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


# ── Podcast RSS ──────────────────────────────────────
def build_feed():
    items = []
    for mp3 in sorted(EPISODES.glob("*.mp3"), reverse=True):
        day = datetime.datetime.strptime(mp3.stem, "%Y-%m-%d").replace(
            hour=9, minute=30, tzinfo=JST)
        note = EPISODES / f"{mp3.stem}.txt"
        summary = note.read_text(encoding="utf-8")[:3000] if note.exists() else ""
        secs = int(duration(mp3))
        items.append(f"""    <item>
      <title>{day.month}月{day.day}日 朝のニュース</title>
      <description>{escape(summary)}</description>
      <pubDate>{format_datetime(day)}</pubDate>
      <guid isPermaLink="false">morning-wave-{mp3.stem}</guid>
      <enclosure url="{SITE}/episodes/{mp3.name}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <itunes:duration>{secs//60}:{secs%60:02d}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>MORNING WAVE</title>
    <link>{SITE}/</link>
    <language>ja</language>
    <description>毎朝、その日のニュースを自動で編成してお届けする個人向けラジオ。</description>
    <itunes:author>MORNING WAVE</itunes:author>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{SITE}/cover.png"/>
    <lastBuildDate>{format_datetime(NOW)}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (DOCS / "feed.xml").write_text(xml, encoding="utf-8")
    print(f"→ feed.xml ({len(items)}本)")


def prune():
    for mp3 in sorted(EPISODES.glob("*.mp3"), reverse=True)[KEEP_DAYS:]:
        mp3.unlink()
        for ext in (".txt", ".script.txt"):
            (EPISODES / f"{mp3.stem}{ext}").unlink(missing_ok=True)


# ── 本編 ─────────────────────────────────────────────
def main():
    EPISODES.mkdir(parents=True, exist_ok=True)
    target_chars = TARGET_MIN * CHARS_PER_MIN

    # 1. 集める
    packs = []
    for seg in SEGMENTS:
        items = collect(seg)
        print(f"◆ {seg['name']}  {len(items)}本")
        for a in items:
            fetch_body(a)
            mark = f"本文{len(a['body'])}字" if a["body"] else "要約のみ"
            print(f"  - {a['title'][:32]}  [{mark}]")
        if items:
            packs.append((seg, items))

    total = sum(len(i) for _, i in packs)
    if not total:
        sys.exit("記事を1件も取得できませんでした。中止します。")

    # 2. 尺から1本あたりの文字数を割り出す（オープニングと導入で約900字）
    per = max(200, min(600, (target_chars - 900) // total))
    print(f"\n記事{total}本 / 目標{target_chars}字 → 1本あたり約{per}字\n")

    # 3. 書く
    lineup = "、".join(s["name"] for s, _ in packs)
    parts = [f"おはようございます。モーニングウェーブ。"
             f"{NOW.year}年{NOW.month}月{NOW.day}日、{'月火水木金土日'[NOW.weekday()]}曜日の朝です。"
             f"きょうは、{lineup}の順にお伝えします。"
             f"それではまいりましょう。"]
    notes = []

    for seg, items in packs:
        print(f"◆ {seg['name']} の原稿")
        s = write_script(seg, items, per)
        parts.append(s["intro"])
        for i, a in enumerate(items):
            line = s["lines"][i] if i < len(s["lines"]) else f"{a['title']}。"
            parts.append(line)
            notes.append(f"・{a['title']}（{a['source']}）\n  {a['link']}")

    parts.append("以上、モーニングウェーブでした。きょうも良い一日を。")

    script = "\n\n".join(parts)          # 空行が読み上げの間になる
    chars = len(script)
    stem = NOW.strftime("%Y-%m-%d")
    (EPISODES / f"{stem}.txt").write_text("\n".join(notes), encoding="utf-8")
    (EPISODES / f"{stem}.script.txt").write_text(script, encoding="utf-8")

    # 4. 焼く
    print(f"\n→ 音声合成中… 台本{chars}字（予想 約{chars/CHARS_PER_MIN:.0f}分）")
    mp3 = EPISODES / f"{stem}.mp3"
    asyncio.run(synth(script, mp3))

    secs = duration(mp3)
    print(f"→ {mp3.name}  {mp3.stat().st_size/1e6:.1f} MB  {secs/60:.1f}分")
    if secs:
        print(f"   実測 {chars/(secs/60):.0f} 字/分 "
              f"（CHARS_PER_MIN={CHARS_PER_MIN} と離れていたら書き換えてください）")

    prune()
    build_feed()


if __name__ == "__main__":
    main()
