"""読み上げてから、利用者の発言を聞き取って文字にする。聞き取りの方式は 2 つある。

- key(既定): キー(右コマンド)を押している間だけ聞く。離すと発言の終わり。
  読み上げは、キーを押したときだけ止まる。押していない間の音は捨て、文字にしない
- word: 合言葉(既定は「以上」)が聞こえるまで、マイクを閉じずに聞き続ける。
  読み上げ中に声が続いたら、読み上げを止める。キーボードに手が届かないときに使う

voice.sh listen から、VoiceMode と一緒に入っている Python で実行する。

- --say を付けると、先にマイクを開いてから Mac 内の Kokoro で読み上げる。
  読み上げ中に声が続いたら、読み上げを止めて聞き取りに入る(割り込み)
- 声の区切りごとに Mac 内の Whisper で文字にし、合言葉で終わったら
  それまでの文字をつなげて標準出力に出す
- 「ミュート」とだけ言われたら、聞き取りをやめて、その旨を出力する

外部に出さないための作り:

- 送信先は Mac 内の Whisper と Kokoro に固定している。変更する引数は設けない
- 音声はメモリ上だけで扱い、ファイルに保存しない
- 認識した文字は、標準出力と、Mac 内の字幕のファイル(本人だけが読める)に出す。
  字幕のファイルは日付ごとに分け、消さずに残す
"""

from __future__ import annotations

import argparse
import array
import collections
import ctypes
import fcntl
import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import wave

import httpx
import webrtcvad

WHISPER_URL = "http://127.0.0.1:2022/v1/audio/transcriptions"  # Mac 内に固定
KOKORO_URL = "http://127.0.0.1:8880/v1/audio/speech"           # Mac 内に固定
KOKORO_VOICE = "jf_alpha"
KOKORO_RATE = 24000  # Kokoro の pcm 出力(モノラル・16bit)
RATE = 16000
FRAME_MS = 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
FRAME_S = FRAME_MS / 1000

GAP_S = 0.8          # この長さの無音で、声の 1 区切りとする
MIN_VOICED_S = 0.3   # 声がこれより短い区切りは雑音として捨てる
MAX_SEGMENT_S = 28   # 区切りがこれを超えたら、いったん文字にする
PREROLL_FRAMES = 10  # 話し始めが欠けないよう、直前 0.3 秒を含める
START_WINDOW = 5     # 直近 5 フレーム中 3 フレームが声なら、話し始めとみなす
START_VOICED = 3
BARGE_VOICED_S = 0.45  # 読み上げ中に声がこの長さ続いたら、割り込みとみなす

# キーの方式。押している間だけ聞く。キーの番号は macOS の仮想キーコード(54 は右コマンド)
TALK_KEY = 54
TALK_KEY_FLAG = 0x10      # 修飾キーの状態に含まれる、右コマンドのビット
RELEASE_GRACE_S = 0.5     # キーを離してから、この秒数のうちに押し直せば、続きとして扱う
KEY_MIN_VOICED_S = 0.09   # キーを押して話した区切りは、声がこの長さあれば文字にする(「はい」のような短い返事を捨てない)
KEY_MIN_HOLD_S = 0.6      # これより短く押しただけなら、ショートカットとみなして、何も鳴らさない
CUE_MISS = "/System/Library/Sounds/Funk.aiff"  # キーを押して話したのに、聞き取れなかったときの合図

# Bluetooth のイヤホンは、マイクを開いた直後に通話用の接続へ切り替わり、
# その間の約 1 秒は音が出ない。音声が届き始めてからこの秒数を待って音を出す
WARMUP_S = 1.5
CUE_START = "/System/Library/Sounds/Ping.aiff"
CUE_END = "/System/Library/Sounds/Glass.aiff"

# 複数のセッションで同時に使うときの順番待ち。ロックを持つセッションだけが話して聞く。
# ロックはプロセスが終わると自動で外れる。ファイルには、最後に使ったセッションの名前だけを書く
LOCK_PATH = os.path.expanduser("~/.voicemode/10-voice.lock")
SPEAK_LOCK_PATH = os.path.expanduser("~/.voicemode/10-voice-speak.lock")  # 読み上げの間だけ持つ。声を重ねないため
LOCK_WAIT_S = 420    # 順番を待つ上限。超えたら、読み上げずに戻る
MIN_LISTEN_S = 120   # 長く待ったあとでも、聞き取りに使える最低限の秒数
FLOOR_HOLD_S = 120   # 時間切れのあと、同じセッションが聞き取りを再開するまで順番を取っておく秒数
TOTAL_LIMIT_S = 540  # 順番待ちと聞き取りを合わせた上限(コマンドの制限時間 600 秒に収める)

# 字幕。読み上げた文と聞き取った文を書き、字幕の窓(caption.swift)が表示する。
# voice.sh の CAPTION_DIR と同じ場所。日付ごとのファイル(YYYY-MM-DD.txt)に追記し、消さない
CAPTION_DIR = os.path.expanduser("~/.voicemode/10-voice-captions")

BOUNDARY = r"\s。、．，.,!?！？…・"
# 間をおかずに「どうぞ以上」と言われても終わりとみなすための、文末らしい文字。
# 「これ以上」「3つ以上」「今まで以上」「必要以上」の直前の文字は含めない
SENTENCE_TAIL = "すたよねぞなだいるんかわて"
MUTE_PATTERN = re.compile(
    rf"^[{BOUNDARY}]*(?:ミュート|mute)(?:して|にして|です|お願い|お願いします)?[{BOUNDARY}]*$",
    re.IGNORECASE,
)


def end_pattern(word: str) -> re.Pattern:
    """区切りの文字が合言葉で終わっているかを判定する正規表現を作る。

    合言葉だけの発話か、文の切れ目のあとに合言葉が来たときに一致する。
    文末らしい文字の直後(「どうぞ以上」)も一致する。
    「三つ以上」「これ以上」のように数量や比較に続く場合は一致しない。
    """
    return re.compile(
        rf"(?:^|(?<=[{BOUNDARY}])|(?<=[{SENTENCE_TAIL}]))[{BOUNDARY}]*{re.escape(word)}(?:です|でーす|だ|だよ)?[{BOUNDARY}]*$"
    )


# セッションの置き場所(優先 / 通常 / 後回し)と状態。字幕の窓(caption.swift)が表示し、
# 利用者がタブをつまんで置き場所を変える。Mac を起動し直すと、最初からになる
SESSIONS_PATH = os.path.expanduser("~/.voicemode/10-voice-sessions.json")
LANES = ("priority", "normal", "later")
_boot: list[str] = []
_flags = {"paused": False}  # 字幕の窓の停止ボタンが押されているか(sessions_update が更新する)


def boot_id() -> str:
    """Mac を起動した時刻。起動し直したら、置き場所の記録を捨てるために使う。"""
    if not _boot:
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"sec = (\d+)", out)
            _boot.append(m.group(1) if m else "")
        except (OSError, subprocess.SubprocessError):
            _boot.append("")
    return _boot[0]


def sessions_update(name: str, state: str | None = None) -> dict:
    """自分のセッションを記録に加え(最初は通常)、状態を書く。全セッションの記録を返す。"""
    try:
        fd = os.open(SESSIONS_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = json.loads(f.read() or "{}")
            except ValueError:
                data = {}
            if data.get("boot") != boot_id() or not isinstance(data.get("sessions"), dict):
                data = {"boot": boot_id(), "sessions": {}}
            me = data["sessions"].setdefault(name, {"lane": "normal", "state": "", "t": 0})
            if me.get("lane") not in LANES:
                me["lane"] = "normal"
            changed = state is not None and me.get("state") != state
            if state is not None:
                me["state"] = state
                me["t"] = int(time.time())
            if changed or me["t"] == 0 or state is not None:
                f.seek(0)
                f.truncate()
                f.write(json.dumps(data, ensure_ascii=False))
            _flags["paused"] = bool(data.get("paused"))
            return data["sessions"]
    except OSError:
        return {name: {"lane": "normal"}}


def session_end(name: str) -> None:
    """会話を終えたセッションを記録から消す。字幕の窓のタブからも消える。"""
    try:
        with open(SESSIONS_PATH, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            data = json.loads(f.read() or "{}")
            if isinstance(data.get("sessions"), dict) and data["sessions"].pop(name, None) is not None:
                f.seek(0)
                f.truncate()
                f.write(json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError):
        pass


def may_speak(sessions: dict, name: str) -> bool:
    """置き場所の決まりで、いま話しかけてよいか。

    停止ボタンが押されている間は、どのセッションも話しかけない。
    優先: いつでも。通常: 優先にほかのセッションがいないとき。後回し: 話しかけない。
    """
    if _flags["paused"]:
        return False
    lane = sessions.get(name, {}).get("lane", "normal")
    if lane == "later":
        return False
    if lane == "normal":
        return not any(v.get("lane") == "priority" for k, v in sessions.items() if k != name)
    return True


def read_floor(f) -> tuple[str, float, bool]:
    """ロックのファイルから (会話中のセッション名, 最後に話し終えた時刻, 会話が続いているか) を読む。"""
    f.seek(0)
    parts = f.read().strip().split("\t")
    try:
        return parts[0], float(parts[1]), parts[2] == "1"
    except (IndexError, ValueError):
        return (parts[0] if parts else ""), 0.0, False


def write_floor(f, name: str, active: bool) -> None:
    f.seek(0)
    f.truncate()
    f.write(f"{name}\t{time.time():.0f}\t{1 if active else 0}")
    f.flush()


def acquire_turn(name: str):
    """順番を取る。戻り値は (ロックのファイル, 直前に使ったのが別のセッションか, 待った秒数)。

    ロックは 2 段になっている。
    - ファイルのロック(flock): 話している、または聞いている間だけ持つ。プロセスが終わると外れる
    - 利用者の番: ファイルの中身に書く。利用者が「以上」を言わないまま聞き取りが時間切れになったとき、
      そのセッションが順番を持ち続ける。利用者が考えている間に、ほかのセッションが話しかけないため。
      同じセッションが聞き取りを再開しないまま FLOOR_HOLD_S 秒たつと、ほかのセッションも話せる
    - 「以上」で話し終えたあと(Claude が作業している間)は、早い者勝ちで、ほかのセッションも話せる

    LOCK_WAIT_S 待っても取れなければ (None, False, 待った秒数) を返す。
    """
    start = time.time()
    f = open(LOCK_PATH, "a+", encoding="utf-8")
    os.chmod(LOCK_PATH, 0o600)
    sessions_update(name, "waiting")
    while True:
        try:
            if not may_speak(sessions_update(name), name):
                raise BlockingIOError  # 置き場所の決まりで、まだ話せない
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            holder, last, active = read_floor(f)
            others_turn = bool(holder) and holder != name and active and time.time() - last < FLOOR_HOLD_S
            if not others_turn:
                write_floor(f, name, True)
                sessions_update(name, "talking")
                return f, bool(holder) and holder != name, time.time() - start
            fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        if time.time() - start >= LOCK_WAIT_S:
            f.close()
            return None, False, time.time() - start
        time.sleep(0.3)


def release_turn(name: str) -> None:
    """会話の順番を手放す。ほかのセッションが、待たずに話せるようになる。"""
    try:
        with open(LOCK_PATH, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            holder, _, _ = read_floor(f)
            if holder == name:
                write_floor(f, name, False)
    except OSError:
        pass


def caption(name: str, who: str, text: str) -> None:
    """字幕のファイルに 1 行足す。字幕が書けなくても会話は止めない。"""
    if not text:
        return
    try:
        # 1 行 1 発言。「時刻<TAB>セッション名<TAB>話し手<TAB>本文」(caption.swift が読む)
        line = "\t".join([time.strftime("%H:%M"), " ".join(name.split()), who, " ".join(text.split())]) + "\n"
        os.makedirs(CAPTION_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(CAPTION_DIR, time.strftime("%Y-%m-%d") + ".txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
    except OSError:
        pass


def session_name(path: str | None = None) -> str:
    """作業フォルダの名前から、先頭の番号と記号を除いたもの(例: 10_設計 → 設計)。"""
    base = os.path.basename((path or os.getcwd()).rstrip("/"))
    return re.sub(r"^[\d_\-\s]+", "", base) or base


_cg: list = []


def key_down() -> bool:
    """話すためのキーが押されているか。調べられない環境では例外を出す。"""
    if not _cg:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGEventSourceKeyState.restype = ctypes.c_bool
        cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
        cg.CGEventSourceFlagsState.restype = ctypes.c_uint64
        cg.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
        _cg.append(cg)
    cg = _cg[0]
    # 0 は、すべての入力元を合わせた状態。キーの状態か、修飾キーのビットのどちらかで判定する
    return bool(cg.CGEventSourceKeyState(0, TALK_KEY)) or bool(cg.CGEventSourceFlagsState(0) & TALK_KEY_FLAG)


def play(path: str) -> None:
    try:
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        pad = b"\x00" * (RATE * 2 * 3 // 10)  # 前後に 0.3 秒の無音を足す。頭の音が欠けて認識されるのを防ぐ
        w.writeframes(pad + pcm + pad)
    return buf.getvalue()


def transcribe(client: httpx.Client, pcm: bytes) -> str:
    r = client.post(
        WHISPER_URL,
        files={"file": ("segment.wav", to_wav(pcm), "audio/wav")},
        data={"model": "whisper-1", "language": "ja", "response_format": "json"},
    )
    r.raise_for_status()
    return " ".join(r.json().get("text", "").split())


class Speaker:
    """Mac 内の Kokoro で音声を作り、途中で止められる形で再生する。"""

    def __init__(self, text: str):
        self.text = text
        self.ready = threading.Event()   # 音声ができた(または失敗した)
        self.done = threading.Event()    # 再生が終わった(または止めた、失敗した)
        self.cancel = threading.Event()
        self.error: str | None = None
        self.pcm = b""
        self.written = 0  # 再生に渡したバイト数
        threading.Thread(target=self._synth, daemon=True).start()

    def _synth(self) -> None:
        try:
            r = httpx.post(
                KOKORO_URL,
                json={"model": "tts-1", "voice": KOKORO_VOICE, "input": self.text, "response_format": "pcm"},
                timeout=120,
            )
            r.raise_for_status()
            self.pcm = r.content
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        self.ready.set()

    def heard_text(self) -> str:
        """止めた時点までに読み上げた部分を、再生した長さの割合から見積もる。

        見積もりなので、直前の文の切れ目まで戻して、控えめに返す。
        """
        if not self.pcm:
            return ""
        played = max(0, self.written - int(0.3 * KOKORO_RATE) * 2)  # 出力の遅れの分を引く
        n = int(len(self.text) * played / len(self.pcm))
        head = self.text[:n]
        cut_at = max(head.rfind(c) for c in "。、？！")
        return head[: cut_at + 1] if cut_at >= 0 else ""

    def start(self) -> None:
        threading.Thread(target=self._play, daemon=True).start()

    def _play(self) -> None:
        import sounddevice as sd

        try:
            self.ready.wait()
            if self.error or self.cancel.is_set():
                return
            step = KOKORO_RATE // 20 * 2  # 0.05 秒ずつ書き、止める指示にすぐ応じる
            # ほかの読み上げ(別のセッションの確認待ちの知らせなど)が終わるのを待つ
            speak_lock = open(SPEAK_LOCK_PATH, "a+", encoding="utf-8")
            os.chmod(SPEAK_LOCK_PATH, 0o600)
            waited = 0.0
            while waited < 60:
                try:
                    fcntl.flock(speak_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if self.cancel.is_set():
                        return
                    time.sleep(0.2)
                    waited += 0.2
            with speak_lock, sd.RawOutputStream(samplerate=KOKORO_RATE, channels=1, dtype="int16") as out:
                for i in range(0, len(self.pcm), step):
                    if self.cancel.is_set():
                        out.abort()
                        return
                    out.write(self.pcm[i : i + step])
                    self.written = min(i + step, len(self.pcm))
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.done.set()


def mic_frames(stop: threading.Event, on_ready):
    import sounddevice as sd

    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(bytes(indata))

    with sd.RawInputStream(samplerate=RATE, blocksize=FRAME_BYTES // 2, dtype="int16", channels=1, callback=callback):
        buf = b""
        received = 0
        ready = False
        while not stop.is_set():
            try:
                chunk = q.get(timeout=0.2)
            except queue.Empty:
                continue
            buf += chunk
            received += len(chunk)
            if not ready and received >= WARMUP_S * RATE * 2:
                ready = True
                on_ready()
            while len(buf) >= FRAME_BYTES:
                yield buf[:FRAME_BYTES]
                buf = buf[FRAME_BYTES:]


def file_frames(path: str, stop: threading.Event):
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            sys.exit("エラー: --file には 16kHz・モノラル・16bit の WAV を指定してください")
        while not stop.is_set():
            frame = w.readframes(FRAME_BYTES // 2)
            if len(frame) < FRAME_BYTES:
                return
            yield frame


NOTIFY_WAIT_S = 600  # 確認待ちの知らせで、順番を待つ上限


def notify() -> int:
    """Claude Code の hook から呼ばれる。実行の確認が画面に出たことを、声で知らせる。

    標準入力の JSON から作業フォルダを読む。そのセッションが音声で会話中のときだけ話す。
    停止ボタンが押されている、後回しに置かれている、ミュート中のときは、黙っている。
    待機中のセッションが話しかけるときと同じ順番待ちをする。ほかのセッションの読み上げと、
    それに続く利用者の発言が終わってから話す。マイクは開かず、利用者の番にも触れない。
    """
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    name = session_name(data.get("cwd") if isinstance(data, dict) else None)

    def current():
        """このセッションの記録。話してはいけない状態なら None。"""
        try:
            with open(SESSIONS_PATH, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return None
        me = rec.get("sessions", {}).get(name) if isinstance(rec.get("sessions"), dict) else None
        if rec.get("boot") != boot_id() or rec.get("paused") or not me:
            return None
        if me.get("lane") == "later" or me.get("state") == "muted":
            return None
        return me.get("state"), me.get("t")

    before = current()
    if before is None:
        return 0
    text = f"{name}、確認待ちです。画面を見てください。"
    speaker = Speaker(text)
    try:
        lock = open(LOCK_PATH, "a+", encoding="utf-8")
    except OSError:
        return 0
    start = time.time()
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            holder, last, active = read_floor(lock)
            if not (holder and holder != name and active and time.time() - last < FLOOR_HOLD_S):
                break  # 順番が空いた。話している間は持ち続け、ほかのセッションを待たせる
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        if time.time() - start >= NOTIFY_WAIT_S:
            return 0
        time.sleep(0.3)
    # 待っている間に、このセッションが次の会話へ進んでいたら、確認はもう済んでいる。知らせない
    if current() != before:
        return 0
    caption(name, "Claude", text)
    speaker.start()
    speaker.done.wait(120)
    if speaker.error:
        play(CUE_MISS)
        time.sleep(1)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="読み上げてから、合言葉が聞こえるまで聞き続け、文字にして出力する")
    ap.add_argument("--say", help="聞き取りの前に読み上げる文。読み上げ中に話すと割り込める")
    ap.add_argument("--name", default=session_name(), help="このセッションの呼び名(既定: 作業フォルダの名前)")
    ap.add_argument("--mode", choices=("key", "word"), default="key", help="聞き取りの方式。key: キーを押している間だけ聞く(既定)。word: 合言葉が聞こえるまで聞く")
    ap.add_argument("--end-word", default="以上", help="発言の終わりを示す合言葉(既定: 以上)")
    ap.add_argument("--idle", type=float, default=300, help="声が聞こえない状態がこの秒数続いたら打ち切る(既定: 300)")
    ap.add_argument("--max", type=float, default=480, help="聞き始めてからこの秒数で打ち切る(既定: 480)")
    ap.add_argument("--release", action="store_true", help="持ち続けている順番を手放して終わる(時間切れのあと、聞き取りを再開しないときに使う)")
    ap.add_argument("--end", action="store_true", help="音声での会話を終える。順番を手放し、字幕の窓のタブから消す")
    ap.add_argument("--notify", action="store_true", help="hook 用。標準入力の JSON にある作業フォルダのセッションが音声で会話中なら、確認待ちを声で知らせる")
    ap.add_argument("--file", help="検証用。マイクの代わりに WAV ファイルを読む")
    ap.add_argument("--verbose", action="store_true", help="区切りごとの認識結果を標準エラーに出す")
    args = ap.parse_args()

    if args.notify:
        return notify()
    if args.release or args.end:
        release_turn(args.name)
        if args.end:
            session_end(args.name)
        return 0

    key_mode = args.mode == "key" and not args.file
    key_note = ""
    if key_mode:
        try:
            key_down()
        except (OSError, AttributeError) as e:
            key_mode = False
            key_note = f"[キーの状態を調べられないため、「{args.end_word}」で終える方式で聞き取りました({type(e).__name__})]"
    pattern = end_pattern(args.end_word)
    vad = webrtcvad.Vad(3)
    stop = threading.Event()
    texts: list[str] = []
    state: dict[str, str | None] = {"reason": None, "error": None}
    jobs: queue.Queue = queue.Queue()

    def worker() -> None:
        with httpx.Client(timeout=120) as client:
            while True:
                pcm = jobs.get()
                try:
                    if pcm is None:
                        return
                    if state["reason"]:
                        continue  # 終了が決まったあとの区切りは捨てる
                    text = transcribe(client, pcm)
                    if args.verbose:
                        print(f"[区切り] {text}", file=sys.stderr)
                    if not text:
                        continue
                    if MUTE_PATTERN.match(text):
                        state["reason"] = "mute"
                        stop.set()
                        continue
                    m = pattern.search(text)
                    if m:
                        head = text[: m.start()].strip()
                        if head:
                            texts.append(head)
                        if not key_mode:  # キーの方式では、習慣で「以上」と言われても、取り除くだけにする
                            state["reason"] = "end-word"
                            stop.set()
                    else:
                        texts.append(text)
                except Exception as e:  # Whisper に届かない場合など
                    state["error"] = f"{type(e).__name__}: {e}"
                    state["reason"] = "error"
                    stop.set()
                finally:
                    jobs.task_done()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def on_signal(signum, frame):
        state["reason"] = state["reason"] or "interrupted"
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    from_file = bool(args.file)
    lock = None
    say = args.say
    if not from_file:
        lock, switched, waited = acquire_turn(args.name)
        if lock is None:
            print("[順番待ち: まだ話せません(ほかのセッションが会話中、または、このセッションより優先のセッションがある、後回しに置かれている)。読み上げていません]")
            return 0
        args.max = max(MIN_LISTEN_S, min(args.max, TOTAL_LIMIT_S - waited))
        if switched and say:
            say = f"{args.name}、からです。{say}"  # 別のセッションから切り替わったときは名乗る
    speaker = Speaker(say) if say and not from_file else None

    def on_ready() -> None:
        if speaker:
            caption(args.name, "Claude", args.say)
            speaker.start()
        else:
            play(CUE_START)

    frames = file_frames(args.file, stop) if from_file else mic_frames(stop, on_ready)

    recent = collections.deque(maxlen=START_WINDOW)
    preroll = collections.deque(maxlen=PREROLL_FRAMES)
    segment: list[bytes] = []
    in_speech = False
    voiced_frames = 0
    silent_run = 0
    elapsed = 0.0
    since_voice = 0.0
    total_frames = 0
    total_voiced = 0
    peak = 0
    speaking = speaker is not None  # 読み上げが終わるまで True
    barged = False
    talking = False   # キーの方式で、キーが押されている(離した直後の猶予を含む)
    release_run = 0   # キーを離してからのフレーム数
    hold_frames = 0   # キーを押し続けているフレーム数
    down = False

    def cut() -> None:
        nonlocal segment, in_speech, voiced_frames, silent_run
        # 読み上げ中の短い物音(割り込みに満たないもの)は捨てる
        min_voiced = KEY_MIN_VOICED_S if key_mode else MIN_VOICED_S
        keep = voiced_frames * FRAME_S >= min_voiced and not (speaking and not barged)
        if keep:
            jobs.put(b"".join(segment))
            if from_file:
                jobs.join()  # 検証では、区切りごとに結果を待って判定の位置をそろえる
        segment, in_speech, voiced_frames, silent_run = [], False, 0, 0

    total_ticks = 0
    for frame in frames:
        total_ticks += 1
        elapsed += FRAME_S
        if key_mode:
            down = key_down()
            if down:
                if not talking and not in_speech:
                    # 押した瞬間から録る。声の判定を待つと、話し始めが欠けることがある
                    in_speech = True
                    segment = list(preroll)
                    voiced_frames = 0
                    silent_run = 0
                    preroll.clear()
                    hold_frames = 0
                talking = True
                hold_frames += 1
                release_run = 0
            elif talking:
                release_run += 1
                if release_run * FRAME_S >= RELEASE_GRACE_S:
                    # キーを離した。話していれば発言の終わり。何も話していなければ、聞き取りを続ける
                    talking = False
                    if in_speech:
                        cut()
                    jobs.join()
                    if state["reason"]:
                        break
                    if texts:
                        state["reason"] = "released"
                        break
                    if hold_frames * FRAME_S >= KEY_MIN_HOLD_S:
                        play(CUE_MISS)  # 押して話したのに、文字にならなかった。もう一度話してもらう
        # キーの方式では、押していない間の音を捨てる(声の判定もしない)
        voiced = vad.is_speech(frame, RATE) if (talking or not key_mode) else False
        recent.append(voiced)
        if args.verbose:
            total_frames += 1
            total_voiced += voiced
            peak = max(peak, max(abs(v) for v in array.array("h", frame)))
        if down if key_mode else voiced:
            since_voice = 0.0
        else:
            since_voice += FRAME_S

        if speaker and speaking:
            # 読み上げの間は、打ち切りの時間を数えない
            elapsed = 0.0
            since_voice = 0.0
            if speaker.done.is_set():
                speaking = False
                if not barged:
                    play(CUE_START)
            elif not barged and (down if key_mode else in_speech and voiced_frames * FRAME_S >= BARGE_VOICED_S):
                barged = True
                speaker.cancel.set()

        if in_speech:
            segment.append(frame)
            if voiced:
                voiced_frames += 1
                silent_run = 0
            else:
                silent_run += 1
            if silent_run * FRAME_S >= GAP_S or len(segment) * FRAME_S >= MAX_SEGMENT_S:
                cut()
        else:
            preroll.append(frame)
            if sum(recent) >= START_VOICED:
                in_speech = True
                segment = list(preroll)
                voiced_frames = sum(recent)
                silent_run = 0
                preroll.clear()

        if stop.is_set():
            break
        if not from_file and total_ticks % 33 == 0:  # 約 1 秒ごとに、停止ボタンを見る
            sessions_update(args.name)
            if _flags["paused"]:
                state["reason"] = state["reason"] or "paused"
                break
        if since_voice >= args.idle and not in_speech and not talking:
            jobs.join()
            state["reason"] = state["reason"] or "idle"
            break
        if elapsed >= args.max:
            state["reason"] = state["reason"] or "max"
            break

    stop.set()
    if speaker:
        speaker.cancel.set()
    if in_speech and not state["reason"]:
        cut()  # ファイルの終わりなど、区切りの途中で止まった分を文字にする
    jobs.join()
    jobs.put(None)
    t.join(timeout=5)
    reason = state["reason"] or "eof"
    if args.verbose:
        print(f"[検証] フレーム数 {total_frames}、声と判定 {total_voiced}、最大音量 {peak}(上限 32767)", file=sys.stderr)

    if reason == "error":
        print(f"エラー: Mac 内の Whisper で文字にできませんでした({state['error']})", file=sys.stderr)
        if texts:
            print("\n".join(texts))
        return 1

    if lock is not None:
        # 利用者が話し終えていない(時間切れ)なら、順番を持ち続ける。同じセッションが聞き取りを再開する。
        # 「以上」やミュートで終わったら手放し、ほかのセッションも話せるようにする
        write_floor(lock, args.name, reason in ("idle", "max", "paused"))
        sessions_update(args.name, "muted" if reason == "mute" else "working")
    if not from_file:
        play(CUE_END)
        said = " ".join(texts)
        if barged:
            said = f"(読み上げに割り込み){said}"
        if reason == "mute":
            said += "(ミュート)"
        caption(args.name, "あなた", said)
    if speaker and speaker.error:
        print(f"[読み上げに失敗しました: {speaker.error}]")
    if barged:
        heard = speaker.heard_text() if speaker else ""
        if heard:
            print(f"[割り込み: 読み上げを途中で止めました。伝わったのは、およそ次の部分までです:「{heard}」]")
        else:
            print("[割り込み: 読み上げを始めてすぐに止めました。ほぼ伝わっていません]")
    if texts:
        print("\n".join(texts))
    if key_note:
        print(key_note)
    waiting_for = "キーは押されていません" if key_mode else f"「{args.end_word}」は聞こえていません"
    idle_what = "キーが押されませんでした" if key_mode else "声が聞こえませんでした"
    notes = {
        "idle": f"[打ち切り: {args.idle:g} 秒間、{idle_what}。{waiting_for}]",
        "max": f"[打ち切り: 制限時間({args.max:g} 秒)に達しました。{'発言の途中かもしれません' if key_mode else waiting_for}]",
        "mute": "[ミュート: 利用者が「ミュート」と言いました。マイクを閉じました]",
        "paused": "[一時停止: 利用者が字幕の窓の停止ボタンを押しました。マイクを閉じました。再開ボタンが押されるまで、どのセッションも話しません]",
        "interrupted": "[中断されました]",
        "eof": f"[音声ファイルの終わりに達しました。「{args.end_word}」は聞こえていません]",
    }
    if reason in notes:
        print(notes[reason])
    return 0


if __name__ == "__main__":
    sys.exit(main())
