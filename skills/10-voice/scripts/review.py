"""話し方の分析のために、1 日分の会話の文字から、利用者の発言と、数の集計を取り出す。

voice.sh review [YYYY-MM-DD] から実行する。日付を省くと、今日の分を使う。
出力は Claude が読み、review.md の手順に従って報告を書く。

- 字幕のファイル(~/.voicemode/10-voice-captions/日付.txt)を読むだけで、書き換えない
- 数の集計を、報告のフォルダ(~/.voicemode/10-voice-reviews/日付.json)に保存する。前回との比較に使う
- どこにも送信しない
"""

from __future__ import annotations

import collections
import json
import os
import re
import statistics
import sys
import time

CAPTION_DIR = os.path.expanduser("~/.voicemode/10-voice-captions")
REVIEW_DIR = os.path.expanduser("~/.voicemode/10-voice-reviews")
USER = "あなた"
MEMO = "メモ"
BARGE_MARK = "(読み上げに割り込み)"
# つなぎの言葉と、文末をぼかす言い回し。単純な文字の一致で数える(癖でない用例も含まれる)
FILLERS = ("なんか", "えっと", "えー", "あのー", "まあ", "ちょっと", "っていう", "みたいな", "とか", "やつ", "感じ")
HEDGES = ("かな", "気がする", "と思う", "かも", "ですかね", "だっけ")
MAX_CONTEXT = 80     # 直前の Claude の発言は、この文字数まで出す
LONG_CHARS = 150     # これ以上の発言を「長い」と数える


def load(date: str) -> list[list[str]]:
    path = os.path.join(CAPTION_DIR, date + ".txt")
    try:
        with open(path, encoding="utf-8") as f:
            return [line.rstrip("\n").split("\t", 3) for line in f if line.count("\t") >= 3]
    except OSError:
        sys.exit(f"エラー: {date} の会話の文字がありません({path})")


def stats(rows: list[list[str]]) -> dict:
    mine = [r for r in rows if r[2] == USER]
    texts = [r[3].replace(BARGE_MARK, "") for r in mine]
    lengths = [len(t) for t in texts] or [0]
    joined = " ".join(texts)
    return {
        "utterances": len(mine),
        "chars": sum(lengths),
        "median_chars": statistics.median(lengths),
        "long_utterances": sum(n >= LONG_CHARS for n in lengths),
        "longest_chars": max(lengths),
        "barge_ins": sum(BARGE_MARK in r[3] for r in mine),
        "memos": sum(r[2] == MEMO for r in rows),
        "sessions": dict(collections.Counter(r[1] for r in mine)),
        "fillers": {w: joined.count(w) for w in FILLERS},
        "hedges": {w: joined.count(w) for w in HEDGES},
    }


def previous(date: str) -> tuple[str, dict] | None:
    """この日より前で、いちばん新しい集計。なければ None。"""
    try:
        names = sorted(n for n in os.listdir(REVIEW_DIR) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", n))
    except OSError:
        return None
    older = [n for n in names if n[:10] < date]
    if not older:
        return None
    try:
        with open(os.path.join(REVIEW_DIR, older[-1]), encoding="utf-8") as f:
            return older[-1][:10], json.load(f)
    except (OSError, ValueError):
        return None


def per_100(count: int, utterances: int) -> float:
    return round(count * 100 / max(1, utterances), 1)


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        sys.exit("使い方: review.py [YYYY-MM-DD]")
    rows = load(date)
    s = stats(rows)
    if s["utterances"] == 0:
        sys.exit(f"{date} には、利用者の発言がありません")

    os.makedirs(REVIEW_DIR, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(REVIEW_DIR, date + ".json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)

    n = s["utterances"]
    print(f"# {date} の集計")
    print(f"報告の保存先: {os.path.join(REVIEW_DIR, date + '.md')}")
    print(f"発言 {n} 件、{s['chars']} 字。1 件の長さの中央値 {s['median_chars']:g} 字、"
          f"{LONG_CHARS} 字以上 {s['long_utterances']} 件、最長 {s['longest_chars']} 字")
    print(f"読み上げへの割り込み {s['barge_ins']} 件({per_100(s['barge_ins'], n)}%)、メモ {s['memos']} 件")
    print("セッション別: " + "、".join(f"{k} {v}" for k, v in s["sessions"].items()))
    print("つなぎの言葉(回数。かっこ内は 100 発言あたり): "
          + "、".join(f"{w} {c}({per_100(c, n)})" for w, c in s["fillers"].items() if c))
    print("文末をぼかす言い回し: " + "、".join(f"{w} {c}({per_100(c, n)})" for w, c in s["hedges"].items() if c))

    prev = previous(date)
    if prev:
        pdate, p = prev
        pn = p.get("utterances", 0)
        print(f"\n# 前回({pdate})との比較(100 発言あたり)")
        print(f"割り込み: {per_100(p.get('barge_ins', 0), pn)} → {per_100(s['barge_ins'], n)}")
        print(f"長い発言: {per_100(p.get('long_utterances', 0), pn)} → {per_100(s['long_utterances'], n)}")
        for group in ("fillers", "hedges"):
            for w, c in s[group].items():
                before = per_100(p.get(group, {}).get(w, 0), pn)
                now = per_100(c, n)
                if before or now:
                    print(f"{w}: {before} → {now}")
    else:
        print("\n(前回の集計は、ありません)")

    print("\n# 発言(時刻、セッション、直前の Claude の発言の冒頭、利用者の発言)")
    last_claude: dict[str, str] = {}
    for when, session, who, text in rows:
        if who == USER:
            context = last_claude.get(session, "")
            print(f"\n[{when} {session}] Claude: {context[:MAX_CONTEXT]}{'…' if len(context) > MAX_CONTEXT else ''}")
            print(f"  利用者: {text}")
        elif who == MEMO:
            print(f"\n[{when} {session}] (メモ): {text}")
        else:
            last_claude[session] = text
    return 0


if __name__ == "__main__":
    sys.exit(main())
