"""研究発表マスターデータの統合・照合・監査.

    python3 tools/presentations/build_master.py

入力（読み取り専用）: researchmap エクスポート JSONL / 産総研時代の業績リスト docx /
研究室HP の content/achievements/_index.md / official_checks.json
出力（Git 管理外の OUTPUT_DIR）:
    presentation_master_draft.json          全件（非公開レコードを含む）
    presentation_master_public_candidates.json  researchmap 非公開を除いた公開候補
    PRESENTATION_RECONCILIATION_REPORT.md
    PRESENTATION_NUMBER_AUDIT.md
    PRESENTATION_REVIEW_REQUIRED.md

講演番号・日付は推測で補わない。情報源どうしが食い違う場合は採用せず issues に記録する。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import sources as src

AS_OF = dt.date(2026, 10, 31)
SELF_NAMES = ("志賀拓麿", "takumashiga", "tshiga", "shigatakuma")
PLACEHOLDER_NUMBERS = {"XXX", "TBD", "xxx", "tbd"}
NUMBER_LABEL_RE = re.compile(
    r"^(?:講演番号[:：]\s*|Presentation ID:\s*|Program ID:\s*|Paper Number:\s*)?"
    r"([A-Za-z0-9][A-Za-z0-9.\-_/]{0,24})$")


# ------------------------------------------------------------------ helpers

def ym(date: str | None) -> int | None:
    if not date:
        return None
    y, m = int(date[:4]), int(date[5:7])
    return y * 12 + m


def coverage(a: str, b: str) -> tuple[float, float]:
    """a（正規化済みタイトル）が b にどれだけ含まれるか：(被覆率, 最長一致の割合)."""
    if not a or not b:
        return 0.0, 0.0
    sm = SequenceMatcher(None, a, b, autojunk=False)
    blocks = sm.get_matching_blocks()
    return sum(x.size for x in blocks) / len(a), max(x.size for x in blocks) / len(a)


def title_score(t: str, other: str) -> float:
    cov, longest = coverage(src.norm(t), src.norm(other))
    # 短い共通文字の寄せ集めで高得点にならないよう、最長一致が短い場合は割り引く
    return cov if longest >= 0.5 else cov * longest * 2


def rm_number(desc: dict | None) -> tuple[str | None, str | None]:
    """researchmap の description から講演番号らしい値を取り出す（明示ラベル付きか短い単語のみ）."""
    if not desc:
        return None, None
    for v in desc.values():
        v = v.strip()
        m = NUMBER_LABEL_RE.match(v)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1), v
    return None, None


def is_self(name: str) -> bool:
    n = src.norm(name)
    return any(s in n for s in SELF_NAMES)


# ------------------------------------------------------------------ researchmap

def load_rm() -> list[dict]:
    out = []
    for d in src.load_researchmap():
        if d["insert"]["type"] != "presentations":
            continue
        m = d["merge"]
        titles = m.get("presentation_title") or {}
        event = m.get("event") or {}
        # タイトルに「， 学会名」が付記されている登録があるため、照合用に除いた版も持つ
        core = {}
        for lang, t in titles.items():
            for ev in event.values():
                t2 = re.sub(r"[,，]\s*" + re.escape(ev) + r"\s*$", "", t)
                if t2 != t:
                    core[lang] = t2.strip()
        num, num_raw = rm_number(m.get("description"))
        presenters = {lang: [p.get("name") for p in ps] for lang, ps in (m.get("presenters") or {}).items()}
        out.append({
            "rm_id": d["insert"]["id"],
            "display": m.get("display"),
            "titles": titles,
            "titles_core": core,
            "event": event,
            "presenters": presenters,
            "presenters_text": m.get("presenters_text"),
            "date": m.get("publication_date"),
            "from": m.get("from_event_date"),
            "to": m.get("to_event_date"),
            "international": m.get("is_international_presentation"),
            "invited": m.get("invited"),
            "type": m.get("presentation_type"),
            "languages": m.get("languages"),
            "location": m.get("location"),
            "country": m.get("address_country"),
            "promoter": m.get("promoter"),
            "urls": [s.get("@id") for s in m.get("see_also", []) if s.get("@id")],
            "number": num,
            "number_raw": num_raw,
            "description_has_text": bool(m.get("description")) and not num,
        })
    return out


def rm_title_variants(r: dict) -> list[str]:
    return [t for t in list(r["titles_core"].values()) + list(r["titles"].values()) if t]


# ------------------------------------------------------------------ matching

def match(rm: list[dict], word: list[dict], ach: list[dict]):
    """候補ペアを列挙し、スコア順に一対一で割り当てる。確定／候補（要確認）を区別する.

    年月差 3 か月以内を通常の候補とし、それを超える場合はタイトルがほぼ同一かつ
    学会名も一致するものだけを「日付不一致の候補」として拾う（確定はしない）。
    国内学会と国際会議で同じ研究を発表している例があるため、国際・国内区分が
    一致する組み合わせを優先し、区分の合わない競合は競合とみなさない。
    """
    def cat(v):
        return {True: "international", False: "domestic"}.get(v)

    def event_in(r, text):
        t = src.norm(text)
        return any(len(src.norm(e)) >= 6 and src.norm(e) in t for e in r["event"].values())

    def pairs_word():
        for r in rm:
            for w in word:
                d = abs(ym(r["date"]) - (w["year"] * 12 + w["month"]))
                if r["from"]:
                    d = min(d, abs(ym(r["from"]) - (w["year"] * 12 + w["month"])))
                s = max(title_score(t, w["raw"]) for t in rm_title_variants(r))
                if s < 0.6 or (d > 3 and not (s >= 0.95 and event_in(r, w["raw"]))):
                    continue
                yield s, d, cat(r["international"]) == w["section"], r["rm_id"], w["word_id"]

    def pairs_ach():
        for r in rm:
            for a in ach:
                if not a["year"]:
                    continue
                d = abs(ym(r["date"]) - (a["year"] * 12 + a["month"]))
                s = max(max(title_score(a["title"], t), title_score(t, a["title"])) for t in rm_title_variants(r))
                if s < 0.6 or (d > 3 and not (s >= 0.95 and event_in(r, a["venue_raw"]))):
                    continue
                yield s, d, cat(r["international"]) == a["section"], r["rm_id"], a["ach_id"]

    result = {}
    for name, gen in (("word", pairs_word), ("ach", pairs_ach)):
        cand = sorted(gen(), key=lambda x: (-x[0], -x[2], x[1]))
        by_rm, by_other = defaultdict(list), defaultdict(list)
        for s, d, ok_cat, a, b in cand:
            by_rm[a].append((s, d, ok_cat, b))
            by_other[b].append((s, d, ok_cat, a))
        used_a, used_b, confirmed, candidates = set(), set(), {}, []
        for s, d, ok_cat, a, b in cand:
            if a in used_a or b in used_b:
                continue
            # 同点に近い競合（同一テーマの別発表など）があれば確定しない
            rivals = [x for x in by_rm[a] + by_other[b]
                      if x[3] not in (a, b) and x[0] >= s - 0.05 and x[2] >= ok_cat]
            ok = s >= 0.85 and d <= 1 and not rivals
            used_a.add(a)
            used_b.add(b)
            if ok:
                confirmed[a] = (b, round(s, 3), d)
            else:
                candidates.append({"rm_id": a, f"{name}_id": b, "score": round(s, 3), "month_diff": d,
                                   "category_agrees": ok_cat, "rivals": [x[3] for x in rivals]})
        for mm in MANUAL_MATCHES:
            other = mm.get(f"{name}_id")
            if not other:
                continue
            candidates = [c for c in candidates if not (c["rm_id"] == mm["researchmap_id"] or c.get(f"{name}_id") == other)]
            confirmed = {k: v for k, v in confirmed.items() if v[0] != other}
            confirmed[mm["researchmap_id"]] = (other, "manual", None)
        result[name] = (confirmed, candidates)
    return result


def rm_duplicates(rm: list[dict]) -> list[tuple]:
    dups = []
    for i, a in enumerate(rm):
        for b in rm[i + 1:]:
            if abs(ym(a["date"]) - ym(b["date"])) > 1:
                continue
            s = max(title_score(x, y) for x in rm_title_variants(a) for y in rm_title_variants(b))
            if s >= 0.9:
                dups.append((a["rm_id"], b["rm_id"], round(s, 3)))
    return dups


# ------------------------------------------------------------------ assembly

def official_for(titles: list[str], checks: list[dict], years: set[int]) -> dict | None:
    # 1〜2 文字の表記差（例：構造転移／構造遷移）でも対応付けられるよう類似度で判定する
    # 似たタイトルの別年の発表（例：Phonons 2015 と ATPC2016）に誤適用しないよう発表年も一致させる
    best = max(((max(title_score(c["title_contains"], t) for t in titles if t), c) for c in checks
                if c.get("year") in years),
               key=lambda x: x[0], default=(0, None))
    return best[1] if best[0] >= 0.9 else None


def status_of(date: str | None, precision: str | None) -> str:
    if not date:
        return "unknown"
    if precision == "month":
        y, m = int(date[:4]), int(date[5:7])
        if (y, m) > (AS_OF.year, AS_OF.month):
            return "upcoming"
        if (y, m) == (AS_OF.year, AS_OF.month):
            return "unknown"
        return "completed"
    return "upcoming" if dt.date.fromisoformat(date) > AS_OF else "completed"


MANUAL_MATCHES = json.loads((Path(__file__).with_name("official_checks.json")).read_text(encoding="utf-8")).get("manual_matches", [])
HINTS = json.loads((Path(__file__).with_name("official_checks.json")).read_text(encoding="utf-8")).get("hints", [])


def build():
    rm = load_rm()
    word = src.parse_word()
    ach = src.parse_achievements()
    checks = json.loads((Path(__file__).with_name("official_checks.json")).read_text(encoding="utf-8"))["checks"]
    m = match(rm, word, ach)
    w_conf, w_cand = m["word"]
    a_conf, a_cand = m["ach"]
    W = {w["word_id"]: w for w in word}
    A = {a["ach_id"]: a for a in ach}
    cand_rm = {c["rm_id"] for c in w_cand + a_cand}
    cand_word = {c["word_id"] for c in w_cand}
    cand_ach = {c["ach_id"] for c in a_cand}

    records = []

    def new_record(pid, base):
        rec = {"id": pid, **base, "issues": [], "sources": {}}
        records.append(rec)
        return rec

    # ---- researchmap を基本台帳とする
    for r in rm:
        w = W.get(w_conf.get(r["rm_id"], (None,))[0])
        a = A.get(a_conf.get(r["rm_id"], (None,))[0])
        titles = r["titles"]
        title_original = r["titles_core"].get("ja") or titles.get("ja") or r["titles_core"].get("en") or titles.get("en")
        rec = new_record(f"rm-{r['rm_id']}", {
            "category": None,
            "authors": r["presenters"].get("ja") or r["presenters"].get("en") or [],
            "authors_en": r["presenters"].get("en"),
            "presenters_text": r["presenters_text"],
            "title_original": title_original,
            "title_ja": r["titles_core"].get("ja") or titles.get("ja"),
            "title_en": r["titles_core"].get("en") or titles.get("en"),
            "event": r["event"].get("ja") or r["event"].get("en"),
            "event_en": r["event"].get("en"),
            "presentation_number": None,
            "proceedings_number": None,
            "presentation_date": None,
            "date_precision": None,
            "event_period": [r["from"], r["to"]] if r["from"] else None,
            "location": (r["location"] or {}).get("ja") or (r["location"] or {}).get("en"),
            "country": r["country"],
            "presentation_type": r["type"],
            "invited": None,
            "status": None,
            "urls": list(r["urls"]),
            "researchmap": {"id": r["rm_id"], "display": r["display"],
                            "is_international_presentation": r["international"],
                            "publication_date": r["date"], "number": r["number"],
                            "number_raw": r["number_raw"], "invited": r["invited"]},
            "word": None,
            "achievements": None,
            "publishable": r["display"] == "disclosed",
            "publishable_reason": "researchmap公開" if r["display"] == "disclosed" else "researchmap非公開（明示的な許可が必要）",
            "match": {},
        })
        if r["rm_id"] in cand_rm:
            rec["issues"].append({"kind": "match_uncertain", "detail": "Word資料またはAchievementsに対応候補があるが確定できない"})
        if w:
            rec["word"] = {"id": w["word_id"], "raw": w["raw"], "section": w["section"], "number": w["number"],
                           "number_kind": w["number_kind"], "date": f"{w['year']}-{w['month']:02d}" + (f"-{w['day']:02d}" if w["day"] else ""),
                           "invited": w["invited"], "tags": w["tags"]}
            sc = w_conf[r["rm_id"]]
            rec["match"]["word"] = {"score": sc[1], "month_diff": sc[2]}
            if sc[1] == "manual":
                mm = next(x for x in MANUAL_MATCHES if x["researchmap_id"] == r["rm_id"])
                rec["match"]["word"]["reason"] = mm["reason"]
                rec["merged_ids"] = [mm["retired_id"]]
        if a:
            rec["achievements"] = {"id": a["ach_id"], "text": a["text"], "section": a["section"],
                                   "number": a["number"], "date": f"{a['year']}-{a['month']:02d}" + (f"-{a['day']:02d}" if a["day"] else ""),
                                   "invited": a["invited"], "links": a["links"]}
            rec["match"]["achievements"] = {"score": a_conf[r["rm_id"]][1], "month_diff": a_conf[r["rm_id"]][2]}
            rec["urls"] += [u for u in a["links"] if u and u not in rec["urls"]]
        years = {int(r["date"][:4])} | ({int(r["from"][:4])} if r["from"] else set())
        resolve(rec, r, w, a, official_for([title_original, *rm_title_variants(r)], checks, years))

    # ---- Word 資料のみ
    matched_word = {v[0] for v in w_conf.values()}
    for w in word:
        if w["word_id"] in matched_word:
            continue
        rec = new_record(f"word-{w['word_id']}", {
            "category": w["section"], "authors": None, "presenters_text": None,
            "title_original": None, "title_ja": None, "title_en": None, "event": None,
            "presentation_number": None, "proceedings_number": None,
            "presentation_date": None, "date_precision": None, "event_period": None,
            "location": None, "country": None, "presentation_type": None, "invited": None,
            "status": None, "urls": [], "researchmap": None,
            "word": {"id": w["word_id"], "raw": w["raw"], "section": w["section"], "number": w["number"],
                     "number_kind": w["number_kind"], "date": f"{w['year']}-{w['month']:02d}" + (f"-{w['day']:02d}" if w["day"] else ""),
                     "invited": w["invited"], "tags": w["tags"]},
            "achievements": None,
            "publishable": None, "publishable_reason": "researchmap未登録（Word資料のみ。著者・タイトルの分割と公開可否の確認が必要）",
            "match": {},
        })
        rec["sources"]["category"] = "word"
        rec["issues"].append({"kind": "word_only", "detail": "researchmapに対応する登録が見つからない"
                              + ("（対応候補あり）" if w["word_id"] in cand_word else "")})
        resolve(rec, None, w, None, None)

    # ---- Achievements のみ
    matched_ach = {v[0] for v in a_conf.values()}
    for a in ach:
        if a["ach_id"] in matched_ach:
            continue
        key = hashlib.sha1((src.norm(a["title"]) + str(a["year"])).encode()).hexdigest()[:10]
        rec = new_record(f"ach-{key}", {
            "category": a["section"], "authors": [x.strip() for x in re.split(r"[,，]\s*(?:and\s+)?", a["authors_raw"]) if x.strip()],
            "presenters_text": None, "title_original": a["title"], "title_ja": None, "title_en": None,
            "event": None, "presentation_number": None, "proceedings_number": None,
            "presentation_date": None, "date_precision": None, "event_period": None,
            "location": None, "country": None, "presentation_type": None, "invited": None,
            "status": None, "urls": a["links"], "researchmap": None, "word": None,
            "achievements": {"id": a["ach_id"], "text": a["text"], "section": a["section"], "number": a["number"],
                             "date": f"{a['year']}-{a['month']:02d}" + (f"-{a['day']:02d}" if a["day"] else ""),
                             "invited": a["invited"], "links": a["links"]},
            "publishable": True, "publishable_reason": "研究室HP（Achievements）に掲載済み",
            "match": {},
        })
        venue = re.sub(r"[（(]\d{4}年.*$", "", a["venue_raw"]).strip(" ，,")
        parts = [x.strip() for x in re.split(r"[，,]", venue) if x.strip()]
        if parts:
            rec["event"], rec["sources"]["event"] = parts[0], "achievements"
            rest = [x for x in parts[1:] if x != a["number"] and x not in PLACEHOLDER_NUMBERS]
            if rest:
                rec["location"], rec["sources"]["location"] = rest[-1], "achievements"
        rec["sources"]["category"] = "achievements"
        rec["issues"].append({"kind": "achievements_only", "detail": "researchmapに対応する登録が見つからない"
                              + ("（対応候補あり）" if a["ach_id"] in cand_ach else "")})
        resolve(rec, None, None, a, official_for([a["title"]], checks, {a["year"]}))

    meta = {
        "as_of": AS_OF.isoformat(),
        "counts": {"researchmap": len(rm), "word": len(word), "achievements": len(ach)},
        "match": {"word_confirmed": len(w_conf), "word_candidates": w_cand,
                  "ach_confirmed": len(a_conf), "ach_candidates": a_cand},
        "researchmap_duplicates": rm_duplicates(rm),
    }
    return records, meta, rm, word, ach


def resolve(rec, r, w, a, off):
    """分類・講演番号・日付・招待区分を決め、出典と不一致を記録する."""
    iss = rec["issues"]
    S = rec["sources"]

    # ---------- 国際・国内区分（researchmap 基本）
    if r is not None:
        if r["international"] is True:
            rec["category"], S["category"] = "international", "researchmap"
        elif r["international"] is False:
            rec["category"], S["category"] = "domestic", "researchmap"
        else:
            alt = (w or {}).get("section") or (a or {}).get("section")
            if alt:
                rec["category"], S["category"] = alt, "word" if w else "achievements"
                iss.append({"kind": "category_unset_in_researchmap",
                            "detail": f"researchmap区分未設定。{'Word資料' if w else 'Achievements'}の区分（{alt}）を仮採用"})
            else:
                rec["category"], S["category"] = "unclassified", None
                iss.append({"kind": "category_unset_in_researchmap", "detail": "researchmap区分未設定で、他資料からも判断できない"})
        for other, name in ((w, "Word資料"), (a, "Achievements")):
            if other and rec["category"] in ("international", "domestic") and other["section"] != rec["category"] \
                    and S["category"] == "researchmap":
                iss.append({"kind": "category_mismatch",
                            "detail": f"researchmap={rec['category']} / {name}={other['section']}"})

    # ---------- 公式資料のタイトル・補足
    if off and off.get("title_official"):
        rec["title_official"] = off["title_official"]
        if rec.get("title_original") and src.norm(rec["title_original"]) != src.norm(off["title_official"]):
            iss.append({"kind": "title_mismatch_official",
                        "detail": f"researchmap等={rec['title_original']} / 公式={off['title_official']}"})
    rec_years = {int(v[:4]) for v in [(r or {}).get("date"), (w and f"{w['year']}"), (a and a["year"] and str(a["year"]))] if v}
    for h in HINTS:
        if h.get("year") in rec_years and any(title_score(h["title_contains"], t) >= 0.9 for t in [rec.get("title_original") or "", (w or {}).get("raw", "")] if t):
            rec.setdefault("notes", []).append(h["note"])

    # ---------- 志賀本人が著者に含まれるか（研究室メンバーのみの発表を区別）
    names = list(rec.get("authors") or []) + ([w["raw"]] if w else [])
    rec["pi_is_author"] = any(is_self(n) or "志賀" in n or "Shiga" in n for n in names)
    if not rec["pi_is_author"]:
        rec["include_in_about_pi"] = False
        iss.append({"kind": "not_pi_presentation", "detail": "志賀拓麿が著者に含まれない（研究室メンバーの発表）"})
    else:
        rec["include_in_about_pi"] = True

    # ---------- 講演番号
    cand = {}
    if w and w["number"]:
        if w["number_kind"] == "presentation_number":
            cand["word"] = w["number"]
        else:
            rec["proceedings_number"], S["proceedings_number"] = w["number"], f"word ({w['number_kind']})"
    if r and r["number"]:
        if src.number_kind(r["number"]) == "presentation_number":
            cand["researchmap"] = r["number"]
        elif not rec["proceedings_number"]:
            rec["proceedings_number"], S["proceedings_number"] = r["number"], "researchmap"
    if a and a["number"] and a["number"] not in PLACEHOLDER_NUMBERS:
        cand["achievements"] = a["number"]
    if off and off.get("presentation_number"):
        cand["official"] = off["presentation_number"]
    rec["number_candidates"] = cand
    vals = {v.replace(" ", "") for v in cand.values()}
    if "official" in cand and len(vals) > 1:
        rec["presentation_number"] = cand["official"]
        S["presentation_number"] = f"official（{off['source']}）"
        iss.append({"kind": "number_mismatch_resolved",
                    "detail": "確認済み資料で確定：" + " / ".join(f"{k}={v}" for k, v in cand.items())})
    elif len(vals) == 1:
        rec["presentation_number"] = next(iter(cand.values()))
        S["presentation_number"] = "+".join(cand)
    elif len(vals) > 1:
        rec["presentation_number"] = None
        S["presentation_number"] = None
        iss.append({"kind": "number_mismatch", "detail": " / ".join(f"{k}={v}" for k, v in cand.items())})
    if a and a["number"] in PLACEHOLDER_NUMBERS:
        iss.append({"kind": "number_placeholder", "detail": f"Achievementsの講演番号が仮表記（{a['number']}）"})

    # ---------- 発表日
    dates = {}
    if off and off.get("presentation_date"):
        dates["official"] = (off["presentation_date"], "day")
    if a and a["year"]:
        dates["achievements"] = (f"{a['year']}-{a['month']:02d}" + (f"-{a['day']:02d}" if a["day"] else ""),
                                 "day" if a["day"] else "month")
    if r:
        dates["researchmap"] = (r["date"], "day")
    if w:
        dates["word"] = (f"{w['year']}-{w['month']:02d}" + (f"-{w['day']:02d}" if w["day"] else ""),
                         "day" if w["day"] else "month")
    rec["date_candidates"] = {k: v[0] for k, v in dates.items()}
    for k in ("official", "achievements", "researchmap", "word"):
        if k in dates:
            rec["presentation_date"], rec["date_precision"] = dates[k]
            S["presentation_date"] = k
            break
    if off and off.get("event_period"):
        rec["event_period"] = off["event_period"]
        S["event_period"] = f"official（{off['source']}）"
    if off and off.get("location"):
        if rec.get("location"):
            rec["location_official"] = off["location"]  # 既存の表記（英語等）は残し、公式表記を併記する
        else:
            rec["location"], S["location"] = off["location"], f"official（{off['source']}）"
    if off and off.get("presentation_type"):
        rec["presentation_type"], S["presentation_type"] = off["presentation_type"], f"official（{off['source']}）"
    if r and S.get("presentation_date") == "researchmap":
        period = off.get("event_period") if off else None
        if period and period[0] <= r["date"] <= period[1]:
            S["presentation_date"] = "researchmap（公式資料の開催期間内の登録日）"
        elif r["from"] and r["from"] <= r["date"] <= r["to"]:
            S["presentation_date"] = "researchmap（開催期間内の登録日）"
        elif r["from"]:
            iss.append({"kind": "date_outside_event_period",
                        "detail": f"researchmap登録日 {r['date']} が開催期間 {r['from']}〜{r['to']} の外"})
        else:
            S["presentation_date"] = "researchmap（開催期間の記録なし。講演日かは未確認）"
    # 日付の食い違い（日精度どうしは日単位、月精度を含む場合は年月で比較）
    chosen = rec["presentation_date"]
    settled = S.get("presentation_date", "").startswith("official") or "公式資料" in S.get("presentation_date", "")
    kind = "date_mismatch_resolved" if settled else "date_mismatch"
    for k, (v, p) in dates.items():
        if k == S.get("presentation_date", "").split("（")[0] or not chosen:
            continue
        if p == "day" and rec["date_precision"] == "day":
            if v != chosen:
                iss.append({"kind": kind, "detail": f"採用={chosen}（{S['presentation_date']}） / {k}={v}"})
        elif v[:7] != chosen[:7]:
            iss.append({"kind": kind, "detail": f"採用={chosen}（{S['presentation_date']}） / {k}={v}（年月）"})
    rec["status"] = status_of(rec["presentation_date"], rec["date_precision"])

    # ---------- 招待講演・発表種別
    inv = {}
    if r is not None:
        inv["researchmap"] = bool(r["invited"]) or r["type"] == "invited_oral_presentation"
    if w:
        inv["word"] = w["invited"]
    if a:
        inv["achievements"] = a["invited"]
    if off and "invited" in off:
        inv["official"] = off["invited"]
    rec["invited_candidates"] = inv
    if inv:
        if len(set(inv.values())) == 1:
            rec["invited"] = next(iter(inv.values()))
            S["invited"] = "+".join(inv)
        else:
            rec["invited"] = None
            iss.append({"kind": "invited_mismatch", "detail": " / ".join(f"{k}={v}" for k, v in inv.items())})
    if w and w["keynote"]:
        rec["presentation_type"] = "keynote"
        S["presentation_type"] = "word"
    if r and r["invited"] is not None and r["type"]:
        if bool(r["invited"]) != (r["type"] == "invited_oral_presentation") and r["type"] != "public_discourse":
            iss.append({"kind": "invited_flag_inconsistent",
                        "detail": f"researchmap invited={r['invited']} / presentation_type={r['type']}"})
    # 招待講演の講演者が本人かを確認（presenters_text の * は講演者）
    if rec["invited"] or any(inv.values()):
        speaker = None
        pt = (r or {}).get("presenters_text") or {}
        for t in pt.values():
            mm = re.search(r"\*\s*([^，,、*]+?)(?:\s+and\s|[，,、]|$)", t)
            if mm:
                speaker = " ".join(mm.group(1).split()[:2]) if re.match(r"[A-Za-z]", mm.group(1)) else mm.group(1).strip()
        authors = rec.get("authors") or []
        first = authors[0] if authors else None
        who = speaker or first
        if who and not is_self(who):
            iss.append({"kind": "invited_speaker_not_self",
                        "detail": f"招待講演の講演者が本人でない可能性（講演者={who}）"})

    # ---------- 開催前か
    if rec["status"] == "upcoming":
        iss.append({"kind": "upcoming", "detail": f"{AS_OF.isoformat()}時点で開催前"})
    for k in ("authors", "title_original", "event", "category"):
        if rec.get(k) and k not in S:
            S[k] = "researchmap" if r else ("achievements" if a else "word")


# ------------------------------------------------------------------ reports

OUTPUT_FILES = ("presentation_master_draft.json", "presentation_master_public_candidates.json",
                "PRESENTATION_RECONCILIATION_REPORT.md", "PRESENTATION_NUMBER_AUDIT.md",
                "PRESENTATION_REVIEW_REQUIRED.md", "PRESENTATION_CHANGELOG.md")
KEY_FIELDS = ("category", "title_original", "event", "presentation_number", "proceedings_number",
              "presentation_date", "date_precision", "event_period", "location", "presentation_type",
              "invited", "status", "publishable", "include_in_about_pi")


def classify(p) -> dict:
    """掲載判断のための区分（集計と各レコードの `listing` に使う）."""
    rmd = (p.get("researchmap") or {}).get("display")
    return {
        "pi_is_author": bool(p.get("pi_is_author")),
        "rm_disclosed": rmd == "disclosed",
        "rm_closed": rmd == "closed",
        "rm_closed_public_elsewhere": rmd == "closed" and bool(p.get("achievements")),
        "word_only": p["id"].startswith("word-"),
        "lab_member_only": not p.get("pi_is_author"),
        "public_candidate": p["publishable"] is True and rmd != "closed",
    }


def listing_status(p) -> str:
    c = classify(p)
    if c["lab_member_only"]:
        return "exclude_lab_member_only"
    if c["public_candidate"]:
        return "about_pi_candidate"
    return "on_hold"


def backup_and_diff(out, records):
    """既存の成果物を backup/<時刻>/ に退避し、前回のマスターとの差分を返す."""
    current = out / "presentation_master_draft.json"
    if not current.exists():
        return None
    # 差分の基準：環境変数 PRESENTATION_BASELINE で指定した版（未指定なら直前の版）
    old_path = Path(os.environ.get("PRESENTATION_BASELINE", current))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = out / "backup" / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    for f in OUTPUT_FILES:
        if (out / f).exists():
            (bdir / f).write_bytes((out / f).read_bytes())
    old = {p["id"]: p for p in json.loads(old_path.read_text(encoding="utf-8"))["presentations"]}
    new = {p["id"]: p for p in records}
    retired = {}
    for p in records:
        for rid in p.get("merged_ids", []):
            retired[rid] = p["id"]
    changed = []
    for pid in sorted(set(old) & set(new)):
        diffs = {k: (old[pid].get(k), new[pid].get(k)) for k in KEY_FIELDS if old[pid].get(k) != new[pid].get(k)}
        oi = sorted(i["kind"] for i in old[pid]["issues"])
        ni = sorted(i["kind"] for i in new[pid]["issues"])
        if oi != ni:
            diffs["issues"] = (oi, ni)
        if diffs:
            changed.append((pid, diffs))
    return {"backup_dir": str(bdir), "baseline": str(old_path), "old_count": len(old), "new_count": len(new),
            "removed": sorted(set(old) - set(new)), "added": sorted(set(new) - set(old)),
            "retired": retired, "changed": changed,
            "old_cat": Counter(p["category"] for p in old.values()),
            "old_public": sum(1 for p in old.values() if p["publishable"] is True
                              and (p.get("researchmap") or {}).get("display") != "closed")}


def write_changelog(out, diff, records):
    L = ["# 統合マスター 更新履歴\n", f"生成：{dt.datetime.now().isoformat(timespec='seconds')}\n"]
    if not diff:
        L.append("前回の成果物がないため差分なし。\n")
    else:
        newcat = Counter(p["category"] for p in records)
        newpub = sum(1 for p in records if classify(p)["public_candidate"])
        L.append(f"更新前の成果物のバックアップ：`{diff['backup_dir']}`\n")
        L.append(f"差分の基準：`{diff['baseline']}`\n")
        L.append("## 件数\n\n| | 更新前 | 更新後 |\n|---|---|---|")
        L.append(f"| 総件数 | {diff['old_count']} | {diff['new_count']} |")
        L.append(f"| 国際 | {diff['old_cat']['international']} | {newcat['international']} |")
        L.append(f"| 国内 | {diff['old_cat']['domestic']} | {newcat['domestic']} |")
        L.append(f"| 公開候補 | {diff['old_public']} | {newpub} |\n")
        L.append("## 廃止したID（重複統合）\n")
        L.append("| 廃止ID | 統合先ID |\n|---|---|")
        for a, b in diff["retired"].items():
            L.append(f"| {a} | {b} |")
        L.append("")
        L.append(f"削除されたID：{', '.join(diff['removed']) or 'なし'}　／　追加されたID：{', '.join(diff['added']) or 'なし'}\n")
        L.append(f"## 内容が変わったレコード（{len(diff['changed'])}件）\n")
        for pid, d in diff["changed"]:
            L.append(f"### {pid}\n")
            for k, (a, b) in d.items():
                L.append(f"- `{k}`：{a} → {b}")
            L.append("")
    (out / "PRESENTATION_CHANGELOG.md").write_text("\n".join(L), encoding="utf-8")


def write_outputs(records, meta, rm, word, ach):
    out = src.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    for p in records:
        p["listing"] = listing_status(p)
    diff = backup_and_diff(out, records)
    meta["retired_ids"] = {mm["retired_id"]: f"rm-{mm['researchmap_id']}" for mm in MANUAL_MATCHES}
    records.sort(key=lambda x: (x["presentation_date"] or "0000"), reverse=True)
    master = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "meta": meta, "presentations": records}
    (out / "presentation_master_draft.json").write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8")
    public = [p for p in records if p["publishable"] is True and (p.get("researchmap") or {}).get("display") != "closed"]
    (out / "presentation_master_public_candidates.json").write_text(json.dumps(
        {"generated_at": master["generated_at"],
         "note": "researchmap非公開レコードとWord資料のみの未確認レコードを除外",
         "presentations": public}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_reconciliation(out, records, meta, rm, word, ach)
    write_number_audit(out, records, word)
    write_review(out, records, meta)
    write_changelog(out, diff, records)
    return out


def _t(p):
    t = p.get("title_original") or (p.get("word") or {}).get("raw", "")
    return (t[:70] + "…") if len(t) > 71 else t


def _date(p):
    d = p.get("presentation_date") or "—"
    return d + ("（年月）" if p.get("date_precision") == "month" else "")


def _private(p):
    return "🔒" if (p.get("researchmap") or {}).get("display") == "closed" else ""


def write_reconciliation(out, records, meta, rm, word, ach):
    c = Counter(p["category"] for p in records)
    issues = Counter(i["kind"] for p in records for i in p["issues"])
    rm_only = [p for p in records if p["researchmap"] and not p["word"] and not p["achievements"]]
    L = []
    L.append("# 研究発表データ照合報告書\n")
    L.append(f"基準日：{AS_OF.isoformat()}　／　生成：`tools/presentations/build_master.py`\n")
    L.append("🔒 = researchmap 非公開レコード（公開用データには含めない）\n")
    L.append("## 1. 件数\n")
    L.append("| 項目 | 件数 |\n|---|---|")
    L.append(f"| researchmap 講演・口頭発表等 | {len(rm)}（国際 {sum(1 for r in rm if r['international'] is True)} / 国内 {sum(1 for r in rm if r['international'] is False)} / 区分未設定 {sum(1 for r in rm if r['international'] is None)}、非公開 {sum(1 for r in rm if r['display']=='closed')}） |")
    L.append(f"| Word資料「4. 学会発表」明細 | {len(word)}（国際 {sum(1 for w in word if w['section']=='international')} / 国内 {sum(1 for w in word if w['section']=='domestic')}） |")
    L.append(f"| Achievements 研究発表（日本語版） | {len(ach)}（国際 {sum(1 for a in ach if a['section']=='international')} / 国内 {sum(1 for a in ach if a['section']=='domestic')}） |")
    L.append(f"| 統合後の発表総数 | {len(records)}（国際 {c['international']} / 国内 {c['domestic']} / 未分類 {c['unclassified']}） |")
    L.append(f"| researchmap ↔ Word 同一と確定 | {meta['match']['word_confirmed']} |")
    L.append(f"| researchmap ↔ Word 候補（未確定） | {len(meta['match']['word_candidates'])} |")
    L.append(f"| researchmap ↔ Achievements 同一と確定 | {meta['match']['ach_confirmed']} |")
    L.append(f"| researchmap ↔ Achievements 候補（未確定） | {len(meta['match']['ach_candidates'])} |")
    L.append(f"| researchmap のみ | {len(rm_only)} |")
    L.append(f"| Word資料のみ | {sum(1 for p in records if p['id'].startswith('word-'))} |")
    L.append(f"| Achievements のみ | {sum(1 for p in records if p['id'].startswith('ach-'))} |")
    L.append(f"| 開催前（{AS_OF.isoformat()} 時点） | {sum(1 for p in records if p['status']=='upcoming')} |")
    L.append("")
    K = [classify(p) for p in records]
    L.append("## 1-2. 掲載判断のための区分\n")
    L.append("| 区分 | 件数 |\n|---|---|")
    for label, n in [
        ("全登録発表", len(records)),
        ("志賀拓麿が著者に含まれる発表", sum(k["pi_is_author"] for k in K)),
        ("researchmap で公開設定", sum(k["rm_disclosed"] for k in K)),
        ("researchmap で非公開", sum(k["rm_closed"] for k in K)),
        ("　うち他の情報源（Achievements）で公開済み", sum(k["rm_closed_public_elsewhere"] for k in K)),
        ("Word資料のみ", sum(k["word_only"] for k in K)),
        ("研究室メンバーのみ（志賀拓麿が著者でない）", sum(k["lab_member_only"] for k in K)),
        ("公開候補（researchmap 非公開と未確認の Word 資料のみを除く）", sum(k["public_candidate"] for k in K)),
        ("About PI 掲載候補（公開候補のうち志賀拓麿が著者）", sum(1 for p in records if p["listing"] == "about_pi_candidate")),
        ("掲載判断を保留（志賀拓麿が著者だが researchmap 非公開または Word 資料のみ）", sum(1 for p in records if p["listing"] == "on_hold")),
    ]:
        L.append(f"| {label} | {n} |")
    L.append("")
    L.append(f"整合性：公開候補 {sum(k['public_candidate'] for k in K)} = About PI 掲載候補 "
             f"{sum(1 for p in records if p['listing'] == 'about_pi_candidate')} + 研究室メンバーのみ "
             f"{sum(1 for p in records if p['listing'] == 'exclude_lab_member_only' and classify(p)['public_candidate'])}。"
             f"全登録 {len(records)} = About PI 掲載候補 + 保留 {sum(1 for p in records if p['listing'] == 'on_hold')} + 研究室メンバーのみ "
             f"{sum(1 for p in records if p['listing'] == 'exclude_lab_member_only')}。\n")
    L.append("照合方法：タイトル（日本語・英語の各表記、学会名付記を除いた版）の正規化後の被覆率と、"
             "発表年月の差で候補を抽出。被覆率 0.85 以上・年月差 1 か月以内・競合候補なしの場合のみ確定とし、"
             "それ以外は候補（要確認）とした。\n")
    L.append("## 2. 不一致・要確認の内訳\n")
    L.append("| 種類 | 件数 |\n|---|---|")
    for k, v in sorted(issues.items(), key=lambda x: -x[1]):
        L.append(f"| `{k}` | {v} |")
    L.append("")

    def table(title, rows, cols):
        L.append(f"## {title}（{len(rows)}件）\n")
        if not rows:
            L.append("なし\n")
            return
        L.append("| " + " | ".join(h for h, _ in cols) + " |")
        L.append("|" + "---|" * len(cols))
        for p in rows:
            L.append("| " + " | ".join(str(f(p)).replace("|", "／") for _, f in cols) + " |")
        L.append("")

    base = [("ID", lambda p: p["id"] + _private(p)), ("区分", lambda p: p["category"]),
            ("日付", _date), ("タイトル", _t), ("学会", lambda p: p.get("event") or "")]
    table("3. researchmap のみに存在する発表", rm_only, base)
    table("4. Word資料のみに存在する発表", [p for p in records if p["id"].startswith("word-")],
          [("ID", lambda p: p["id"]), ("区分", lambda p: p["category"]), ("年月", _date),
           ("明細（抜粋）", lambda p: p["word"]["raw"][:110] + "…")])
    table("5. Achievements のみに存在する発表", [p for p in records if p["id"].startswith("ach-")],
          [("ID", lambda p: p["id"]), ("区分", lambda p: p["category"]), ("日付", _date), ("タイトル", _t)])
    L.append(f"## 6. 重複の疑い（researchmap 内、{len(meta['researchmap_duplicates'])}組）\n")
    for a, b, s in meta["researchmap_duplicates"]:
        L.append(f"- rm-{a} ↔ rm-{b}（類似度 {s}）")
    L.append("" if meta["researchmap_duplicates"] else "なし\n")
    sel = lambda kind: [p for p in records if any(i["kind"] == kind for i in p["issues"])]
    det = lambda kind: (lambda p: "；".join(i["detail"] for i in p["issues"] if i["kind"] == kind))
    for n, (title, kind) in enumerate([("国際・国内区分の不一致", "category_mismatch"),
                                       ("researchmap 区分未設定", "category_unset_in_researchmap"),
                                       ("発表日の不一致", "date_mismatch"),
                                       ("開催期間外の登録日", "date_outside_event_period"),
                                       ("確認済み資料で解消した日付の不一致（researchmap / Word資料の修正推奨）", "date_mismatch_resolved")], start=7):
        table(f"{n}. {title}", sel(kind), base[:4] + [("内容", det(kind))])
    # タイトル不一致：確定照合のうちタイトルが完全一致しないもの
    rows = []
    for p in records:
        for k in ("word", "achievements"):
            mt = p["match"].get(k)
            if mt and (mt["score"] == "manual" or mt["score"] < 0.999):
                rows.append((p, k, mt["score"]))
    L.append(f"## 12. 発表タイトルの表記差（確定照合のうち完全一致でないもの、{len(rows)}件）\n")
    L.append("| ID | 照合先 | 類似度 | researchmap タイトル | 照合先の記載 |\n|---|---|---|---|---|")
    for p, k, s in sorted(rows, key=lambda x: (x[2] == "manual", 0 if x[2] == "manual" else x[2])):
        other = p[k]["raw"] if k == "word" else p[k]["text"]
        L.append(f"| {p['id']}{_private(p)} | {k} | {s} | {_t(p)} | {other[:90].replace('|','／')}… |")
    L.append("")
    (out / "PRESENTATION_RECONCILIATION_REPORT.md").write_text("\n".join(L), encoding="utf-8")


def write_number_audit(out, records, word):
    L = ["# 講演番号の補完状況\n", f"基準日：{AS_OF.isoformat()}\n",
         "講演番号は推測で生成していない。情報源間で異なる場合は採用せず「不一致」とした。"
         "プロシーディングスの論文番号・掲載ページは講演番号と区別して `proceedings_number` に記録した。\n"]
    by = lambda p: p["category"]
    rows = []
    for cat in ("international", "domestic", "unclassified"):
        P = [p for p in records if p["category"] == cat]
        rows.append((cat, len(P),
                     sum(1 for p in P if p["presentation_number"]),
                     sum(1 for p in P if "word" in p.get("number_candidates", {}) and p["presentation_number"]),
                     sum(1 for p in P if "word" in p.get("number_candidates", {}) and p["presentation_number"]
                         and not (p.get("researchmap") or {}).get("number")),
                     sum(1 for p in P if (p.get("researchmap") or {}).get("number")),
                     sum(1 for p in P if "achievements" in p.get("number_candidates", {})),
                     sum(1 for p in P if any(i["kind"] == "number_mismatch" for i in p["issues"])),
                     sum(1 for p in P if not p["presentation_number"])))
    L.append("## 1. 集計\n")
    L.append("| 区分 | 発表数 | 番号確定 | うちWord資料由来 | うちWordで新たに補完（researchmapに番号なし） | researchmapに元々番号 | Achievementsに番号 | 不一致 | 未確認 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        L.append("| " + " | ".join(map(str, r)) + " |")
    L.append("")

    def sect(title, P, cols):
        L.append(f"## {title}（{len(P)}件）\n")
        if not P:
            L.append("なし\n")
            return
        L.append("| " + " | ".join(c for c, _ in cols) + " |")
        L.append("|" + "---|" * len(cols))
        for p in P:
            L.append("| " + " | ".join(str(f(p)).replace("|", "／") for _, f in cols) + " |")
        L.append("")

    cols = [("ID", lambda p: p["id"] + _private(p)), ("区分", lambda p: p["category"]), ("日付", _date),
            ("講演番号", lambda p: p["presentation_number"] or ""), ("出典", lambda p: p["sources"].get("presentation_number") or ""),
            ("タイトル", _t)]
    sect("2. Word資料から補完できた講演番号",
         [p for p in records if "word" in p.get("number_candidates", {}) and p["presentation_number"]], cols)
    sect("3. researchmap 側に元々存在する講演番号",
         [p for p in records if (p.get("researchmap") or {}).get("number")],
         cols[:3] + [("researchmap 記載", lambda p: p["researchmap"]["number_raw"]), ("採用", lambda p: p["presentation_number"] or "（不一致のため保留）"), ("タイトル", _t)])
    sect("4. Achievements 側に存在する講演番号",
         [p for p in records if p.get("achievements") and p["achievements"].get("number")],
         cols[:3] + [("Achievements", lambda p: p["achievements"]["number"]), ("採用", lambda p: p["presentation_number"] or ""), ("タイトル", _t)])
    K5 = ("number_mismatch", "number_placeholder", "number_mismatch_resolved")
    sect("5. 番号の不一致（未解決・仮表記・確認済み資料で解消したもの）",
         [p for p in records if any(i["kind"] in K5 for i in p["issues"])],
         cols[:3] + [("採用", lambda p: p["presentation_number"] or "（保留）"), ("出典", lambda p: p["sources"].get("presentation_number") or ""),
                     ("内容", lambda p: "；".join(i["detail"] for i in p["issues"] if i["kind"] in K5)), ("タイトル", _t)])
    sect("6. プロシーディングス番号・掲載ページ（講演番号とは別扱い）",
         [p for p in records if p.get("proceedings_number")],
         cols[:3] + [("記載", lambda p: p["proceedings_number"]), ("種別", lambda p: p["sources"].get("proceedings_number")), ("タイトル", _t)])
    sect("7. 講演番号が未確認の発表",
         [p for p in records if not p["presentation_number"]], cols[:3] + [("タイトル", _t), ("学会", lambda p: p.get("event") or "")])
    (out / "PRESENTATION_NUMBER_AUDIT.md").write_text("\n".join(L), encoding="utf-8")


REVIEW_PRIORITY = {
    "number_mismatch": (1, "講演番号が情報源間で異なる"),
    "match_uncertain": (1, "対応関係を確定できない"),
    "category_mismatch": (2, "国際・国内区分が情報源間で異なる"),
    "invited_mismatch": (2, "招待講演の扱いが情報源間で異なる"),
    "invited_speaker_not_self": (2, "招待講演の講演者が本人でない可能性"),
    "date_mismatch": (2, "発表日が情報源間で異なる"),
    "category_unset_in_researchmap": (3, "researchmap の区分が未設定"),
    "date_outside_event_period": (3, "researchmap の登録日が開催期間外"),
    "invited_flag_inconsistent": (3, "researchmap の招待フラグと発表種別が矛盾"),
    "number_placeholder": (3, "講演番号が仮表記"),
    "word_only": (3, "Word資料のみ（researchmap 未登録）"),
    "achievements_only": (3, "Achievements のみ（researchmap 未登録）"),
    "title_mismatch_official": (2, "タイトルが公式資料と異なる"),
    "not_pi_presentation": (3, "志賀拓麿が著者でない（About PI 対象外）"),
}

NEEDS = {
    "number_mismatch": "公式プログラムで講演番号を確認",
    "match_uncertain": "タイトル・学会・年月を比較して同一発表か判断",
    "category_mismatch": "学会の性格（国際会議か）を確認して researchmap か Word のどちらが正しいか判断",
    "invited_mismatch": "招待状・公式プログラムで招待講演か確認",
    "invited_speaker_not_self": "本人が講演したか確認（共著者の招待講演なら本人の招待講演に数えない）",
    "date_mismatch": "公式プログラムで実際の講演日を確認",
    "category_unset_in_researchmap": "researchmap 側で区分を設定",
    "date_outside_event_period": "researchmap の登録日か開催期間の誤りを確認",
    "invited_flag_inconsistent": "researchmap の招待フラグまたは発表種別を修正",
    "number_placeholder": "公式プログラム公開後に講演番号を確認",
    "word_only": "researchmap への登録漏れか、掲載対象外かを判断",
    "achievements_only": "researchmap への登録漏れか確認",
    "title_mismatch_official": "researchmap 側のタイトルを公式表記に更新するか判断",
    "not_pi_presentation": "About PI には掲載しない（Achievements でのみ扱う）ことを確認",
}


def write_review(out, records, meta):
    items = []
    for p in records:
        ks = [i for i in p["issues"] if i["kind"] in REVIEW_PRIORITY]
        if not ks:
            continue
        pri = min(REVIEW_PRIORITY[i["kind"]][0] for i in ks)
        items.append((pri, p, ks))
    items.sort(key=lambda x: (x[0], x[1]["presentation_date"] or ""), reverse=False)
    L = ["# 要確認事項一覧\n", f"基準日：{AS_OF.isoformat()}　／　全 {len(items)} 件（重要度 1 が最優先）\n",
         "🔒 = researchmap 非公開レコード\n"]
    pri_names = {1: "重要度 1（番号・同一性）", 2: "重要度 2（区分・日付・招待）", 3: "重要度 3（登録・表記）"}
    cur = None
    for pri, p, ks in items:
        if pri != cur:
            cur = pri
            L.append(f"\n## {pri_names[pri]}\n")
        L.append(f"### {p['id']}{_private(p)}　{_t(p)}\n")
        L.append(f"- 区分：{p['category']}　日付：{_date(p)}（{p['sources'].get('presentation_date')}）　状態：{p['status']}")
        if p.get("researchmap"):
            r = p["researchmap"]
            L.append(f"- researchmap：id={r['id']}、登録日={r['publication_date']}、国際={r['is_international_presentation']}、番号={r['number_raw'] or 'なし'}、招待={r['invited']}")
        if p.get("word"):
            L.append(f"- Word資料（{p['word']['id']}）：{p['word']['raw']}")
        if p.get("achievements"):
            L.append(f"- Achievements（{p['achievements']['id']}）：{p['achievements']['text'][:200]}")
        for n in p.get("notes", []):
            L.append(f"- 補足：{n}")
        for i in ks:
            L.append(f"- **{REVIEW_PRIORITY[i['kind']][1]}**：{i['detail']}　→ 確認に必要：{NEEDS[i['kind']]}")
        ids = {("rm", (p.get("researchmap") or {}).get("id")), ("word", (p.get("word") or {}).get("id")),
               ("ach", (p.get("achievements") or {}).get("id"))}
        cands = [c for c in meta["match"]["word_candidates"] + meta["match"]["ach_candidates"]
                 if {("rm", c["rm_id"]), ("word", c.get("word_id")), ("ach", c.get("ach_id"))}
                 & {x for x in ids if x[1]}]
        for c in cands:
            L.append(f"- 対応候補：rm-{c['rm_id']} ↔ {c.get('word_id') or c.get('ach_id')}（類似度 {c['score']}、年月差 {c['month_diff']}"
                     + (f"、競合 {', '.join(c['rivals'])}" if c["rivals"] else "") + "）")
        L.append("")
    (out / "PRESENTATION_REVIEW_REQUIRED.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    recs, meta, rm, word, ach = build()
    out = write_outputs(recs, meta, rm, word, ach)
    c = Counter(p["category"] for p in recs)
    print(f"統合 {len(recs)} 件（国際 {c['international']} / 国内 {c['domestic']} / 未分類 {c['unclassified']}）")
    print(f"researchmap↔Word 確定 {meta['match']['word_confirmed']} / 候補 {len(meta['match']['word_candidates'])}")
    print(f"researchmap↔Achievements 確定 {meta['match']['ach_confirmed']} / 候補 {len(meta['match']['ach_candidates'])}")
    print(f"出力先: {out}")
