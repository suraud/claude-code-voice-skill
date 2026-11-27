"""listen.py の、声を使わずに確かめられる部分のテスト。

マイクもスピーカーも使わない。Whisper と Kokoro にも接続しない。
ホームを一時フォルダに差し替えてから listen.py を読み込むので、利用者の ~/.voicemode には触れない。
実行は tests/run.sh から行う(単独なら: python tests/test_listen.py)。
"""
import os
import stat
import sys
import tempfile
import time
import unicodedata
import unittest

_home = tempfile.mkdtemp(prefix="10-voice-test-")
os.environ["HOME"] = _home
os.makedirs(os.path.join(_home, ".voicemode"), mode=0o700)
# 利用者の読みがなの表(テスト用)。listen.py は、最初に使うときに 1 回だけ読む
with os.fdopen(os.open(os.path.join(_home, ".voicemode", "10-voice-yomi.tsv"), os.O_WRONLY | os.O_CREAT, 0o600), "w", encoding="utf-8") as f:
    f.write("# テスト用\nWi-Fi\tワイファイ\nPostgreSQL\tポスグレ\tポストグレス\nINT\tイント\n")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "10-voice", "scripts"))
import listen as L  # noqa: E402


def mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class Paths(unittest.TestCase):
    def test_home_is_replaced(self):
        # 以降のテストが、利用者のファイルに触れないことの確認
        for p in (L.SESSIONS_PATH, L.CAPTION_DIR, L.MEMO_DIR, L.YOMI_USER_PATH, L.LOCK_PATH, L.DEBUG_DIR):
            self.assertTrue(p.startswith(_home), p)

    def test_services_are_local_only(self):
        for url in (L.WHISPER_URL, L.KOKORO_URL):
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)


class Numbers(unittest.TestCase):
    def test_kanji_number(self):
        cases = {"0": "〇", "2": "二", "10": "十", "16": "十六", "100": "百", "110": "百十", "2026": "二千二十六",
                 "10000": "一万", "12345": "一万二千三百四十五", "007": "〇〇七", "123456789": "一二三四五六七八九"}
        for digits, want in cases.items():
            self.assertEqual(L.kanji_number(digits), want, digits)

    def test_numbers_in_sentence(self):
        cases = {"2 つあります": "二つあります", "0.8 秒": "れいてん八秒", "1.25 倍": "一てん二五倍", "16 件": "十六件"}
        for text, want in cases.items():
            shown, spoken, _ = L.split_say(text)
            self.assertEqual(shown, text)       # 字幕には、算用数字のまま出す
            self.assertEqual(spoken, want)

    def test_numbers_inside_words_are_kept(self):
        for text in ("v1.2.0", "abc123", "x86"):
            self.assertEqual(L.split_say(text)[1], text)


class Yomi(unittest.TestCase):
    def test_ruby(self):
        shown, spoken, _ = L.split_say("{listen.py|リッスンパイ}を直しました")
        self.assertEqual(shown, "listen.pyを直しました")
        self.assertEqual(spoken, "リッスンパイを直しました")

    def test_table_word(self):
        shown, spoken, _ = L.split_say("GitHub に送りました")
        self.assertEqual(shown, "GitHub に送りました")
        self.assertEqual(spoken, "ギットハブ に送りました")

    def test_table_ignores_case_and_prefers_longer(self):
        self.assertEqual(L.split_say("github")[1], "ギットハブ")
        self.assertEqual(L.split_say("README.md")[1], "リードミーエムディー")

    def test_table_does_not_match_inside_word(self):
        self.assertEqual(L.split_say("Gitter")[1], "Gitter")   # 「Git」を当てはめない

    def test_user_table_wins(self):
        self.assertEqual(L.split_say("Wi-Fi")[1], "ワイファイ")

    def test_position_mapping(self):
        shown, spoken, points = L.split_say("GitHub に送りました")
        self.assertEqual(L.to_shown(points, 0), 0)
        self.assertEqual(L.to_shown(points, len(spoken)), len(shown))
        self.assertEqual(L.to_shown(points, len("ギットハブ")), len("GitHub"))
        self.assertEqual(L.to_shown(points, 10 ** 6), len(shown))


class Notation(unittest.TestCase):
    def test_reading_to_word(self):
        self.assertEqual(L.to_notation("ワイファイがつながらない"), "Wi-Fiがつながらない")

    def test_loose_spelling(self):
        for text in ("wifi", "WIFI", "wi fi", "Wi-Fi"):
            self.assertEqual(L.to_notation(text), "Wi-Fi", text)

    def test_registered_mishearing(self):
        self.assertEqual(L.to_notation("ポストグレスを使う"), "PostgreSQLを使う")

    def test_not_inside_katakana_word(self):
        self.assertEqual(L.to_notation("ポイントは 3 つ"), "ポイントは 3 つ")

    def test_not_inside_ascii_word(self):
        self.assertEqual(L.to_notation("print"), "print")   # INT を当てはめない


class Patterns(unittest.TestCase):
    def test_end_word(self):
        p = L.end_pattern("以上")
        for text in ("以上", "以上。", "以上です", "どうぞ、以上。", "お願いします以上", "これで終わりです。以上です。"):
            self.assertTrue(p.search(text), text)
        for text in ("3つ以上", "これ以上", "今まで以上", "必要以上", "以上のことから", "三つ以上あります"):
            self.assertFalse(p.search(text), text)

    def test_mute(self):
        for text in ("ミュート", "ミュートして。", "ミュートお願いします", "mute"):
            self.assertTrue(L.MUTE_PATTERN.match(text), text)
        for text in ("ミュートの話をします", "ミュートは使わない", ""):
            self.assertFalse(L.MUTE_PATTERN.match(text), text)

    def test_memo(self):
        self.assertEqual(L.MEMO_PATTERN.match("メモ、見積もりを作り直す")[1], "見積もりを作り直す")
        self.assertEqual(L.MEMO_PATTERN.match("めも 牛乳を買う")[1], "牛乳を買う")
        for text in ("メモを見せて", "メモ帳を開いて", "メモリが足りない", "メモって何", "さっきのメモ"):
            self.assertFalse(L.MEMO_PATTERN.match(text), text)


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"text": self.text}


class FakeClient:
    """Whisper の代わり。渡された手がかり(prompt)を覚えて、決めた文を返す。"""

    def __init__(self, text):
        self.text = text
        self.prompt = None
        self.url = None

    def post(self, url, files=None, data=None):
        self.url = url
        self.prompt = (data or {}).get("prompt")
        return FakeResponse(self.text)


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(L.RATE * seconds)


class Transcribe(unittest.TestCase):
    def test_short_fix(self):
        self.assertEqual(L.transcribe(FakeClient("おい。"), silence(1.0)), "はい。")

    def test_short_fix_only_whole_segment(self):
        self.assertEqual(L.transcribe(FakeClient("おい、ちょっと待って"), silence(1.0)), "おい、ちょっと待って")

    def test_hallucination_is_dropped(self):
        self.assertEqual(L.transcribe(FakeClient("ご視聴ありがとうございました。"), silence(1.0)), "")

    def test_notation_applied(self):
        self.assertEqual(L.transcribe(FakeClient("ワイファイを確認して"), silence(1.0)), "Wi-Fiを確認して")

    def test_context_only_for_long_segment(self):
        said = "期限は 12 月 4 日でよいですか。"
        short, long_ = FakeClient("はい"), FakeClient("はい、それで")
        L.transcribe(short, silence(L.CONTEXT_MIN_S - 0.5), said)
        L.transcribe(long_, silence(L.CONTEXT_MIN_S + 0.5), said)
        self.assertNotIn("期限", short.prompt or "")       # 短い区切りには、語の並びを渡す
        self.assertEqual(long_.prompt, said)                # 長い区切りには、直前の読み上げを渡す

    def test_context_is_cut_to_tail(self):
        said = "あ" * 500 + "最後の文です。"
        c = FakeClient("はい、それで")
        L.transcribe(c, silence(3.0), said)
        self.assertEqual(len(c.prompt), L.CONTEXT_MAX_CHARS)
        self.assertTrue(c.prompt.endswith("最後の文です。"))

    def test_echoed_prompt_is_dropped(self):
        said = "期限は来週でよいですか"
        self.assertEqual(L.transcribe(FakeClient(said + "。"), silence(3.0), said), "")

    def test_sends_only_to_local_whisper(self):
        c = FakeClient("はい")
        L.transcribe(c, silence(1.0))
        self.assertEqual(c.url, L.WHISPER_URL)

    def test_no_recording_kept_by_default(self):
        L.transcribe(FakeClient("はい"), silence(1.0))
        self.assertFalse(os.path.exists(L.DEBUG_DIR))   # 調査用のフォルダを作らない限り、録音は残らない


class Turns(unittest.TestCase):
    def setUp(self):
        L._flags.update(paused=False, focus=None)

    def tearDown(self):
        L._flags.update(paused=False, focus=None)

    def test_may_speak(self):
        sessions = {"a": {"lane": "normal"}, "b": {"lane": "later"}}
        self.assertTrue(L.may_speak(sessions, "a"))
        self.assertFalse(L.may_speak(sessions, "b"))          # 「あとで」は話しかけない
        self.assertTrue(L.may_speak(sessions, "new"))         # 記録にないセッションは「いま話す」
        L._flags["focus"] = "b"
        self.assertFalse(L.may_speak(sessions, "a"))          # 話す相手が選ばれている間は、その相手だけ
        self.assertTrue(L.may_speak(sessions, "b"))
        L._flags["paused"] = True
        self.assertFalse(L.may_speak(sessions, "b"))          # 停止の間は、だれも話さない

    def test_held_by_other(self):
        now = time.time()
        sessions = {"a": {"state": "waiting"}, "b": {"state": "away"}}
        self.assertTrue(L.held_by_other("me", "a", now, True, sessions))
        self.assertFalse(L.held_by_other("me", "a", now, False, sessions))                    # 会話が続いていない
        self.assertFalse(L.held_by_other("me", "a", now - L.FLOOR_HOLD_S - 1, True, sessions))  # 取っておく時間を過ぎた
        self.assertFalse(L.held_by_other("me", "me", now, True, sessions))                    # 自分
        self.assertFalse(L.held_by_other("me", "b", now, True, sessions))                     # 手放したセッション
        self.assertFalse(L.held_by_other("me", "gone", now, True, sessions))                  # 終えたセッション
        self.assertFalse(L.held_by_other("me", "", now, True, sessions))

    def test_floor_round_trip(self):
        with tempfile.TemporaryFile("w+", encoding="utf-8") as f:
            self.assertEqual(L.read_floor(f), ("", 0.0, False))
            L.write_floor(f, "設計", True)
            name, last, active = L.read_floor(f)
            self.assertEqual((name, active), ("設計", True))
            self.assertAlmostEqual(last, time.time(), delta=5)
            L.write_floor(f, "調査", False)
            self.assertEqual(L.read_floor(f)[::2], ("調査", False))
            f.seek(0)
            f.truncate()
            f.write("こわれた\tx")
            f.flush()
            self.assertEqual(L.read_floor(f), ("こわれた", 0.0, False))


class Files(unittest.TestCase):
    """Mac の中に残すファイルは、本人だけが読める権限で作る。"""

    def test_memo_round_trip_and_mode(self):
        L.memo_add("テスト", "  見積もりを   作り直す ")
        L.memo_add("テスト", "")
        L.memo_add("テスト", "牛乳を買う")
        self.assertEqual(mode(L.MEMO_DIR), 0o700)
        self.assertEqual(mode(os.path.join(L.MEMO_DIR, "テスト.txt")), 0o600)
        self.assertEqual(L.memo_take("テスト"), ["見積もりを 作り直す", "牛乳を買う"])
        self.assertEqual(L.memo_take("テスト"), [])       # 取り出したら、預かり場所から消える
        self.assertEqual(L.memo_take("ない"), [])

    def test_caption_mode_and_format(self):
        L.caption("テスト", "あなた", "こん\tにちは\n続き")
        self.assertEqual(mode(L.CAPTION_DIR), 0o700)
        path = os.path.join(L.CAPTION_DIR, time.strftime("%Y-%m-%d") + ".txt")
        self.assertEqual(mode(path), 0o600)
        with open(path, encoding="utf-8") as f:
            line = f.read().splitlines()[-1]
        self.assertEqual(line.split("\t")[1:], ["テスト", "あなた", "こん にちは 続き"])   # 1 行 1 発言。タブと改行は入らない

    def test_record_round_trip_and_mode(self):
        if os.path.exists(L.SESSIONS_PATH):
            os.remove(L.SESSIONS_PATH)
        L.record_set("mode", "word")
        self.assertEqual(mode(L.SESSIONS_PATH), 0o600)
        self.assertEqual(L.read_record().get("mode"), "word")
        self.assertEqual(L.listen_mode(), "word")
        L.record_set("mode", "key")
        self.assertEqual(L.listen_mode(), "key")

    def test_broken_record_is_tolerated(self):
        with open(L.SESSIONS_PATH, "w", encoding="utf-8") as f:
            f.write("{こわれた")
        self.assertIsNone(L.read_record())
        self.assertEqual(L.listen_mode(), "key")
        L.record_set("mode", "key")                         # こわれていても、書き直せる
        self.assertEqual(L.read_record(), {"mode": "key"})

    def test_yomi_add(self):
        table = L.YOMI_USER_PATH
        self.assertEqual(L.yomi_add("Kubernetes", "クバネティス"), 0)
        self.assertEqual(L.yomi_add("kubernetes", "クーバネティス", ["久部寝ティス"]), 0)   # 同じ表記は書き換える
        self.assertEqual(L.yomi_add("Kubernetes", "クーバネティス", ["クバネテス"]), 0)     # 聞き間違いは引き継ぐ
        with open(table, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("\t") for line in f if line.lower().startswith("kubernetes")]
        self.assertEqual(rows, [["Kubernetes", "クーバネティス", "久部寝ティス", "クバネテス"]])
        self.assertEqual(L.yomi_add("", "エー"), 2)
        self.assertEqual(L.yomi_add("Kubernetes", " "), 2)
        self.assertEqual(L.yomi_add("a\tb", "エー\nビー"), 0)   # タブと改行は空白に直して、表の列と行をこわさない
        with open(table, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines()[-1], "a b\tエー ビー")
        self.assertEqual(mode(table) & 0o077, 0)

    def test_norm_path(self):
        nfd = unicodedata.normalize("NFD", os.path.join(_home, "ガイド.html"))
        self.assertEqual(L.norm_path(nfd), unicodedata.normalize("NFC", os.path.realpath(nfd)))


class Safety(unittest.TestCase):
    """音声を Mac の外へ出さない、置き場所の外を触らない、何でも開かない。"""

    def test_client_ignores_proxy_settings(self):
        os.environ["HTTP_PROXY"] = os.environ["ALL_PROXY"] = "http://proxy.invalid:8080"
        try:
            with L.local_client() as client:
                self.assertFalse(client.trust_env)
                self.assertEqual(client._mounts, {})      # プロキシへの経路が、1 つも入っていない
        finally:
            del os.environ["HTTP_PROXY"], os.environ["ALL_PROXY"]

    def test_every_http_call_ignores_proxy_settings(self):
        # httpx を直接呼ぶ箇所には、すべて trust_env=False を付ける(付け忘れると、プロキシへ送られる)
        import re
        with open(L.__file__, encoding="utf-8") as f:
            src = f.read()
        calls = [m.start() for m in re.finditer(r"httpx\.(?:post|get|put|request|stream|Client|AsyncClient)\(", src)]
        self.assertTrue(calls)
        for pos in calls:
            depth, end = 0, pos
            for end in range(src.index("(", pos), len(src)):
                depth += {"(": 1, ")": -1}.get(src[end], 0)
                if depth == 0:
                    break
            self.assertIn("trust_env=False", src[pos:end], src[pos:pos + 60])

    def test_safe_name(self):
        cases = {"設計": "設計", "../../x": "_.._x", "a/b": "a_b", ".hidden": "hidden", "..": "_", "a\\b": "a_b", "": "_"}
        for name, want in cases.items():
            self.assertEqual(L.safe_name(name), want, name)

    def test_memo_stays_inside_its_folder(self):
        outside = os.path.join(_home, "outside.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("消えてはいけない\n")
        name = "../../outside"
        L.memo_add(name, "メモ")
        self.assertEqual(os.path.dirname(os.path.realpath(L.memo_path(name))), os.path.realpath(L.MEMO_DIR))
        self.assertEqual(L.memo_take(name), ["メモ"])
        with open(outside, encoding="utf-8") as f:
            self.assertEqual(f.read(), "消えてはいけない\n")

    def test_rename_rejects_empty(self):
        self.assertEqual(L.session_rename("テスト", "   "), 2)
        self.assertEqual(L.session_rename("テスト", "/"), 2)

    def test_openable(self):
        d = tempfile.mkdtemp(dir=_home)
        paths = {}
        for name in ("報告.html", "図.PNG", "run.command", "tool.sh", "メモ"):
            paths[name] = os.path.join(d, name)
            open(paths[name], "w").close()
        os.mkdir(os.path.join(d, "Evil.app"))
        os.symlink(paths["run.command"], os.path.join(d, "偽.html"))
        for ok in (paths["報告.html"], paths["図.PNG"], "https://example.com/a", "http://127.0.0.1:8080/"):
            self.assertTrue(L.openable(ok), ok)
        for ng in (paths["run.command"], paths["tool.sh"], paths["メモ"], os.path.join(d, "Evil.app"), d,
                   os.path.join(d, "偽.html"), os.path.join(d, "ない.html"),
                   "smb://example.com/share", "file:///etc/passwd", "javascript:alert(1)", "vscode://x", "ssh://example.com",
                   "https://", ""):
            self.assertFalse(L.openable(ng), ng)

    def test_is_listen(self):
        self.assertFalse(L.is_listen(os.getpid()))      # このテストのプロセスは、listen.py ではない
        self.assertFalse(L.is_listen("abc"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
