"""研究発表データ統合用：元資料の読み込み（読み取り専用）.

元資料（researchmap エクスポート、産総研時代の業績リスト）はリポジトリ外に置き、
ここでは読むだけで一切変更しない。出力は OUTPUT_DIR（Git 管理外）にのみ書き出す。
パスは環境変数で上書きできる。
"""

from __future__ import annotations

import html
import json
import os
import re
import unicodedata
import zipfile
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parents[2]

RESEARCHMAP_JSONL = Path(os.environ.get(
    "RM_JSONL", HOME / "Desktop" / "rm_researchers20261031.jsonl"))
WORD_CANDIDATES = [
    HOME / "Desktop" / "2_研究教育業績リスト2024_志賀拓麿_産総研(1).docx",
    HOME / "Desktop" / "2_研究教育業績リスト2024_志賀拓麿_産総研.docx",
]
ACHIEVEMENTS_MD = REPO / "content" / "achievements" / "_index.md"
OUTPUT_DIR = Path(os.environ.get(
    "PRESENTATION_AUDIT_DIR", HOME / "Desktop" / "presentation_audit"))


def word_path() -> Path:
    if os.environ.get("WORD_DOCX"):
        return Path(os.environ["WORD_DOCX"])
    for p in WORD_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError("研究教育業績リストの docx が見つかりません")


def norm(s: str | None) -> str:
    """照合用の正規化：NFKC・小文字化・空白と記号の除去."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("‐", "-").replace("–", "-").replace("—", "-")
    return re.sub(r"[\s\W_]+", "", s)


# ---------------------------------------------------------------- Word 資料

_DATE_RE = re.compile(
    r"[（(]\s*(\d{4})\s*[年月]\s*(\d{1,2})\s*月\s*(?:(\d{1,2})\s*日)?\s*[）)]")
_TAG_RE = re.compile(r"【([^】]+)】")
# 講演番号らしい語（英数字と . - _ / のみで数字を含む）
_NUMBER_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9.\-_/]{0,24}$")
_PAGES_RE = re.compile(r"^\d{3,}-\d{3,}$")                 # 例: 6045-6051（掲載ページ）
_PAPER_NO_RE = re.compile(r"^(SHTC|PRTEC|IHTC|HT|IMECE|MNHMT)\d*-\d+", re.I)  # 例: SHTC2021-69161


def number_kind(num: str | None) -> str | None:
    """講演番号と、プロシーディングスの論文番号・掲載ページを区別する."""
    if not num:
        return None
    if _PAGES_RE.match(num):
        return "pages"
    if "/" in num and _PAPER_NO_RE.match(num):
        return "paper_number+presentation_code"
    if _PAPER_NO_RE.match(num):
        return "paper_number"
    return "presentation_number"


def _paragraphs(docx: Path) -> list[tuple[str, bool]]:
    xml = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
    body = xml[xml.index("<w:body>"):]
    out = []
    for m in re.finditer(r"<w:p[ >].*?</w:p>", body, re.S):
        p = m.group(0)
        text = "".join(html.unescape(t) for t in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
        out.append((text, "<w:numPr>" in p))
    return out


def parse_word() -> list[dict]:
    """「4. 学会発表」の国際学会・国内学会の明細を返す（文書中の順序を保持）."""
    paras = _paragraphs(word_path())
    items, section = [], None
    for text, numbered in paras:
        if text.startswith("(1)国際学会"):
            section = "international"
            continue
        if text.startswith("(2)国内学会"):
            section = "domestic"
            continue
        if text.startswith("５.") or text.startswith("5."):
            section = None
        if section and numbered and text.strip():
            items.append(_parse_word_item(text.strip(), section, len(
                [i for i in items if i["section"] == section]) + 1))
    return items


def _parse_word_item(raw: str, section: str, seq: int) -> dict:
    tags = _TAG_RE.findall(raw)
    body = _TAG_RE.sub("", raw).strip()
    year = month = day = None
    date_raw = None
    m = None
    for m in _DATE_RE.finditer(body):
        pass
    if m:
        date_raw = m.group(0)
        year, month = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if m.group(3) else None
        before = body[:m.start()]
    else:
        before = body
    # 日付直前の 1〜2 区切り（カンマ・読点）に講演番号らしい語があれば候補とする
    segs = [s.strip() for s in re.split(r"[,，、]", before)]
    number = None
    for s in reversed([s for s in segs if s][-2:]):
        if _NUMBER_RE.match(s) and not re.fullmatch(r"\d{4}", s):
            number = s
            break
    prefix = "W-I" if section == "international" else "W-D"
    return {
        "word_id": f"{prefix}{seq:03d}",
        "section": section,
        "seq": seq,
        "raw": raw,
        "year": year, "month": month, "day": day,
        "date_raw": date_raw,
        "number": number,
        "number_kind": number_kind(number),
        "invited": any("招待" in t for t in tags),
        "keynote": any("基調" in t for t in tags),
        "tags": tags,
    }


# ---------------------------------------------------------------- Achievements

def parse_achievements() -> list[dict]:
    """研究室HP 研究業績ページ（日本語版）の国際・国内研究発表."""
    md = ACHIEVEMENTS_MD.read_text(encoding="utf-8")
    out = []
    fy = None
    section = None
    for block in re.split(r"\n(?=#{2,3} )", md):
        head = block.splitlines()[0]
        m = re.match(r"## (\d{4})年度", head)
        if m:
            fy = int(m.group(1))
            continue
        if head.startswith("### 国際研究発表"):
            section = "international"
        elif head.startswith("### 国内研究発表"):
            section = "domestic"
        else:
            section = None
        if not section:
            continue
        for i, li in enumerate(re.findall(r"<li>(.*?)</li>", block, re.S), 1):
            lines = [l.strip() for l in li.strip().split("<br>")]
            text = re.sub(r"<[^>]+>", "", li)
            text = html.unescape(re.sub(r"\s+", " ", text)).strip()
            venue = lines[2] if len(lines) > 2 else ""
            dm = re.search(r"（(\d{4})年(\d{1,2})月(\d{1,2}|xx)日）", venue)
            num = re.search(r"<strong>([^<]+)</strong>", venue)
            out.append({
                "ach_id": f"A-FY{fy}-{'I' if section == 'international' else 'D'}{i:02d}",
                "fiscal_year": fy,
                "section": section,
                "authors_raw": html.unescape(re.sub(r"<[^>]+>", "", lines[0])).strip(" ,，"),
                "title": html.unescape(re.sub(r"<[^>]+>", "", lines[1] if len(lines) > 1 else "")).strip(" ,，"),
                "venue_raw": html.unescape(re.sub(r"<[^>]+>", "", venue)).strip(),
                "number": num.group(1) if num and num.group(1) not in ("【招待講演】",) else None,
                "year": int(dm.group(1)) if dm else None,
                "month": int(dm.group(2)) if dm else None,
                "day": int(dm.group(3)) if dm and dm.group(3) != "xx" else None,
                "links": re.findall(r'href="([^"]+)"', li),
                "invited": "招待講演" in li,
                "text": text,
            })
    return out


# ---------------------------------------------------------------- researchmap

def load_researchmap() -> list[dict]:
    """researchmap エクスポート JSONL の講演・口頭発表等（presentations）."""
    recs = []
    with open(RESEARCHMAP_JSONL, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            recs.append(d)
    return recs
