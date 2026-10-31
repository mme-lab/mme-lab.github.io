"""About PI 掲載用の公開データ data/presentations.json を生成する.

    python3 tools/presentations/export_public.py

入力：OUTPUT_DIR/presentation_master_draft.json（build_master.py の出力、Git 管理外）
出力：data/presentations.json（Hugo の公開データ。リポジトリで管理する）

掲載対象は listing == "about_pi_candidate" のレコードのみ。出力する項目は許可リストで
限定し、researchmap 非公開レコードや内部の監査情報が混入していないことを毎回検証する。
"""

from __future__ import annotations

import json
import re
import sys

import sources as src

OUT = src.REPO / "data" / "presentations.json"
ITEM_KEYS = {"id", "category", "year", "date", "date_precision", "authors", "title",
             "event", "event_en", "number", "paper_number", "location", "urls", "invited"}
AUTHOR_KEYS = {"name", "self"}
SELF_PATTERNS = ("志賀拓麿", "takumashiga", "tshiga")


def is_self(name: str) -> bool:
    return any(p in src.norm(name) for p in SELF_PATTERNS)


def verified_day(p: dict) -> bool:
    """講演日（日単位）が裏付けられているか。researchmap の登録日だけでは日単位にしない."""
    if p["date_precision"] != "day":
        return False
    basis = p["sources"].get("presentation_date") or ""
    if basis.startswith(("official", "achievements")):
        return True
    if basis == "word":
        return True
    period = p.get("event_period")
    # 1 日だけの開催なら登録日＝講演日
    return bool(period and period[0] == period[1] == p["presentation_date"])


def to_public(p: dict) -> dict:
    day = verified_day(p)
    date = p["presentation_date"] if day else p["presentation_date"][:7]
    invited = bool(p["invited"]) and not any(i["kind"] == "invited_speaker_not_self" for i in p["issues"])
    item = {
        "id": p["id"],
        "category": p["category"],
        "year": int(p["presentation_date"][:4]),
        "date": date,
        "date_precision": "day" if day else "month",
        "authors": [{"name": a, "self": is_self(a)} for a in p["authors"]],
        "title": p.get("title_official") or p["title_original"],
        "event": p["event"],
        "event_en": p.get("event_en"),
        "number": p["presentation_number"],          # 講演・ポスター番号のみ（表示用）
        "paper_number": p.get("paper_number"),       # プロシーディングス論文番号（現在は表示しない）
        "location": p["location"],
        "urls": [u for u in p["urls"] if u.startswith(("http://", "https://"))],
        "invited": invited,
    }
    return {k: v for k, v in item.items() if v not in (None, [], "")} | {"invited": invited}


def validate(items: list[dict], master: list[dict]) -> None:
    errors = []
    by_id = {p["id"]: p for p in master}
    expected = [p["id"] for p in master if p["listing"] == "about_pi_candidate"]
    ids = [i["id"] for i in items]
    if sorted(ids) != sorted(expected):
        errors.append("掲載IDが about_pi_candidate と一致しない")
    if len(set(ids)) != len(ids):
        errors.append("IDが重複している")
    closed = [p for p in master if (p.get("researchmap") or {}).get("display") == "closed"]
    blob = json.dumps(items, ensure_ascii=False)
    for p in closed:
        if p["id"] in ids:
            errors.append(f"非公開レコード {p['id']} が含まれている")
        for t in (p.get("title_original"), p.get("title_ja"), p.get("title_en")):
            # 非公開レコードと同じ発表が他の公開レコードに存在することはないため、タイトル一致は混入とみなす
            if t and len(t) > 10 and t in blob:
                errors.append(f"非公開レコード {p['id']} のタイトルが含まれている")
    for i in items:
        src_rec = by_id[i["id"]]
        if src_rec["id"].startswith("word-"):
            errors.append(f"Word資料のみのレコード {i['id']} が含まれている")
        if not any(a["self"] for a in i["authors"]):
            errors.append(f"{i['id']} に志賀拓麿が著者として含まれない")
        if set(i) - ITEM_KEYS or any(set(a) - AUTHOR_KEYS for a in i["authors"]):
            errors.append(f"{i['id']} に許可されていない項目がある")
        if i.get("number") and re.fullmatch(r"(?i)xxx|tbd", i["number"]):
            errors.append(f"{i['id']} の講演番号が仮表記")
        # 未確定候補・ボード番号・論文番号を講演番号として出していないこと
        not_talk = {u["value"] for u in src_rec.get("number_candidates_unverified", [])} \
            | {src_rec.get("board_number"), src_rec.get("paper_number"), src_rec.get("proceedings_number")} - {None}
        if i.get("number") in not_talk:
            errors.append(f"{i['id']} の講演番号に講演番号以外の番号（{i['number']}）が入っている")
        if i.get("paper_number") and not src_rec["sources"].get("paper_number", "").startswith("official"):
            errors.append(f"{i['id']} の論文番号が確認済み資料由来でない")
        if src_rec["sources"].get("presentation_date", "").startswith("researchmap") and i["date_precision"] == "day" \
                and not (src_rec.get("event_period") and src_rec["event_period"][0] == src_rec["event_period"][1]):
            errors.append(f"{i['id']} で未確認の researchmap 登録日を日付として表示している")
    if re.search(r"/Users/|Desktop|presentation_audit|\.jsonl|\.docx", blob):
        errors.append("ローカルパス等が含まれている")
    if errors:
        sys.exit("検証エラー:\n  " + "\n  ".join(errors))


def main() -> None:
    master = json.loads((src.OUTPUT_DIR / "presentation_master_draft.json").read_text(encoding="utf-8"))["presentations"]
    items = [to_public(p) for p in master if p["listing"] == "about_pi_candidate"]
    items.sort(key=lambda i: (i["date"], i["id"]), reverse=True)
    validate(items, master)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    cats = {c: sum(1 for i in items if i["category"] == c) for c in ("international", "domestic")}
    print(f"{OUT.relative_to(src.REPO)}: {len(items)} 件（国際 {cats['international']} / 国内 {cats['domestic']}）"
          f"、日付まで表示 {sum(1 for i in items if i['date_precision'] == 'day')} 件、検証 OK")


if __name__ == "__main__":
    main()
