"""読み上げてから、利用者の発言を聞き取って文字にする。聞き取りの方式は 2 つある。

- key(既定): キー(右コマンド)を押している間だけマイクを開いて聞く。離すと発言の終わり。
  読み上げは、キーを押したときだけ止まる。押していない間は、マイクを閉じている
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
SPEEDS = (1.0, 1.25)  # 字幕の窓のボタンで切り替える、読み上げの速さ
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
KEY_WARMUP_S = 1.0   # キーの方式で、キーを押してから合図を鳴らすまでの秒数(短すぎると、話し始めが欠ける)
BUILTIN_WARMUP_S = 0.3   # Mac 本体のマイクを使うときの待ち(通話用の接続への切り替えがないので、短くてよい)
WARMUP_S = 1.5
CUE_START = "/System/Library/Sounds/Ping.aiff"
CUE_END = "/System/Library/Sounds/Glass.aiff"

# 複数のセッションで同時に使うときの順番待ち。ロックを持つセッションだけが話して聞く。
# ロックはプロセスが終わると自動で外れる。ファイルには、最後に使ったセッションの名前だけを書く
LOCK_PATH = os.path.expanduser("~/.voicemode/10-voice.lock")
# マイクを閉じた時刻を残すファイル。Bluetooth のイヤホンは、マイクを閉じた直後に音楽用の接続へ戻り、
# その間の音が乱れる。読み上げは、最後にマイクを閉じてから MIC_SETTLE_S たつのを待って始める
MIC_CLOSED_PATH = os.path.expanduser("~/.voicemode/10-voice-mic-closed")
MIC_SETTLE_S = 1.5
# 再生の位置(Speaker が書き、字幕の窓のつまみが読む)と、つまみからの位置の指定(字幕の窓が書く)
PLAYBACK_PATH = os.path.expanduser("~/.voicemode/10-voice-playback.json")
SEEK_PATH = os.path.expanduser("~/.voicemode/10-voice-seek.json")
SPEAK_LOCK_PATH = os.path.expanduser("~/.voicemode/10-voice-speak.lock")  # 読み上げの間だけ持つ。声を重ねないため
LOCK_WAIT_S = 420    # 順番を待つ上限。超えたら、読み上げずに戻る
MIN_LISTEN_S = 120   # 長く待ったあとでも、聞き取りに使える最低限の秒数
FLOOR_HOLD_S = 120   # 時間切れのあと、同じセッションが聞き取りを再開するまで順番を取っておく秒数
TOTAL_LIMIT_S = 540  # 順番待ちと聞き取りを合わせた上限(コマンドの制限時間 600 秒に収める)

# 字幕。読み上げた文と聞き取った文を書き、字幕の窓(caption.swift)が表示する。
# voice.sh の CAPTION_DIR と同じ場所。日付ごとのファイル(YYYY-MM-DD.txt)に追記し、消さない
CAPTION_DIR = os.path.expanduser("~/.voicemode/10-voice-captions")

# 声のメモ。聞き取り中に「メモ、…」と言われた分と、Claude の作業中に見張り(--memo-watch)が聞いた分を、
# セッションごとのファイルに預かる。そのセッションが次に聞き取りを終えるときに、出力に付けて渡し、消す
MEMO_DIR = os.path.expanduser("~/.voicemode/10-voice-memos")
MEMO_WATCH_LOCK = os.path.expanduser("~/.voicemode/10-voice-memo-watch.lock")
MEMO_MIN_HOLD_S = 0.8    # 見張りは、キーがこの秒数押され続けてからマイクを開く(ショートカットで開かないため)
CUE_MEMO = "/System/Library/Sounds/Pop.aiff"   # メモを預かった合図

BOUNDARY = r"\s。、．，.,!?！？…・"
# 間をおかずに「どうぞ以上」と言われても終わりとみなすための、文末らしい文字。
# 「これ以上」「3つ以上」「今まで以上」「必要以上」の直前の文字は含めない
SENTENCE_TAIL = "すたよねぞなだいるんかわて"
MUTE_PATTERN = re.compile(
    rf"^[{BOUNDARY}]*(?:ミュート|mute)(?:して|にして|です|お願い|お願いします)?[{BOUNDARY}]*$",
    re.IGNORECASE,
)


# 「メモ、見積もりを作り直す」のように、メモで始まる発言。「メモを見せて」「メモ帳」などは含めない
MEMO_PATTERN = re.compile(rf"^[{BOUNDARY}]*(?:メモ|めも|memo)(?![をのにはがでと帳書]|って|用紙|リ|して)[{BOUNDARY}]*(.*)$", re.IGNORECASE)


def memo_add(name: str, text: str) -> None:
    """セッション宛てのメモを預かる。"""
    text = " ".join(text.split())
    if not text:
        return
    os.makedirs(MEMO_DIR, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(MEMO_DIR, name + ".txt"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(text + "\n")


def memo_take(name: str) -> list[str]:
    """セッション宛てに預かっているメモを取り出し、預かり場所から消す。"""
    try:
        with open(os.path.join(MEMO_DIR, name + ".txt"), "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            memos = [line.strip() for line in f if line.strip()]
            f.seek(0)
            f.truncate()
            return memos
    except OSError:
        return []


def end_pattern(word: str) -> re.Pattern:
    """区切りの文字が合言葉で終わっているかを判定する正規表現を作る。

    合言葉だけの発話か、文の切れ目のあとに合言葉が来たときに一致する。
    文末らしい文字の直後(「どうぞ以上」)も一致する。
    「三つ以上」「これ以上」のように数量や比較に続く場合は一致しない。
    """
    return re.compile(
        rf"(?:^|(?<=[{BOUNDARY}])|(?<=[{SENTENCE_TAIL}]))[{BOUNDARY}]*{re.escape(word)}(?:です|でーす|だ|だよ)?[{BOUNDARY}]*$"
    )


# セッションの置き場所(いま話す / あとで)、話す相手、状態。字幕の窓(caption.swift)が表示し、
# 利用者がタブをつまんで置き場所を変え、タブを押して話す相手を選ぶ。Mac を起動し直すと、最初からになる。
# 置き場所の値は normal(いま話す)と later(あとで)。以前の priority は normal として扱う
SESSIONS_PATH = os.path.expanduser("~/.voicemode/10-voice-sessions.json")
LANES = ("normal", "later")
_boot: list[str] = []
# paused: 字幕の窓の停止ボタンが押されているか。focus: 利用者が話す相手に選んだセッション(選んでいなければ None)。
# どちらも sessions_update が更新する
_flags: dict = {"paused": False, "focus": None}


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


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True  # 調べられないときは、動いているものとして扱う


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
                # Mac を起動し直した。セッションの記録は捨て、方式と速さの選択は引き継ぐ
                keep = {k: data[k] for k in ("mode", "speed", "mic") if k in data}
                data = {"boot": boot_id(), "sessions": {}, **keep}
            me = data["sessions"].setdefault(name, {"lane": "normal", "state": "", "t": 0})
            if me.get("lane") not in LANES:
                me["lane"] = "normal"
            # Claude Code のプロセス番号を残す。終了したセッションを、字幕の窓がタブから消すため
            pid = os.environ.get("CLAUDE_PID", "")
            dirty = me["t"] == 0  # 記録に加えたばかり
            if pid.isdigit():
                # 同じ名前のフォルダで複数の Claude Code を使うことがあるので、番号は複数持つ。
                # 動いていない番号は外す。全部が動いていなければ、字幕の窓がタブを消す
                pids = [p for p in me.get("pids", [me["pid"]] if me.get("pid") else []) if pid_alive(p)]
                if int(pid) not in pids:
                    pids.append(int(pid))
                if pids != me.get("pids"):
                    me["pids"] = pids
                    me.pop("pid", None)
                    dirty = True
            if state is not None:
                me["state"] = state
                me["t"] = int(time.time())
                # 待っている、話している間は、聞き取りのプロセス番号を残す。プロセスが消えたら、
                # 字幕の窓が「待っている」表示をやめる(声をあきらめたセッションを、待機中に見せないため)
                if state in ("waiting", "talking"):
                    me["lpid"] = os.getpid()
                else:
                    me.pop("lpid", None)
                dirty = True
            if dirty:
                f.seek(0)
                f.truncate()
                f.write(json.dumps(data, ensure_ascii=False))
            _flags["paused"] = bool(data.get("paused"))
            focus = data.get("focus")
            _flags["focus"] = focus if focus in data["sessions"] else None  # 会話を終えたセッションは相手にしない
            return data["sessions"]
    except OSError:
        return {name: {"lane": "normal"}}


def session_set_state(name: str, state: str) -> None:
    """記録にあるセッションの状態だけを書き換える(メモの見張りが、声でのミュートを反映するために使う)。"""
    try:
        with open(SESSIONS_PATH, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            data = json.loads(f.read() or "{}")
            me = data.get("sessions", {}).get(name) if isinstance(data.get("sessions"), dict) else None
            if me is not None:
                me["state"] = state
                me["t"] = int(time.time())
                f.seek(0)
                f.truncate()
                f.write(json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError):
        pass


def session_end(name: str) -> None:
    """会話を終えたセッションを記録から消す。字幕の窓のタブからも消える。"""
    try:
        with open(SESSIONS_PATH, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            data = json.loads(f.read() or "{}")
            if isinstance(data.get("sessions"), dict) and data["sessions"].pop(name, None) is not None:
                if data.get("focus") == name:
                    data.pop("focus")
                f.seek(0)
                f.truncate()
                f.write(json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError):
        pass


def may_speak(sessions: dict, name: str) -> bool:
    """置き場所と、話す相手の決まりで、いま話しかけてよいか。

    停止ボタンが押されている間は、どのセッションも話しかけない。
    利用者が話す相手を選んでいる間は、そのセッションだけが話す。
    選んでいなければ、「いま話す」のセッションが順番に話し、「あとで」のセッションは話しかけない。
    """
    if _flags["paused"]:
        return False
    if _flags["focus"]:
        return _flags["focus"] == name
    return sessions.get(name, {}).get("lane", "normal") != "later"


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


_cancelled = threading.Event()  # 止める指示(SIGTERM、SIGINT)を受けた


def acquire_turn(name: str, wait_s: float = LOCK_WAIT_S):
    """順番を取る。戻り値は (ロックのファイル, 直前に使ったのが別のセッションか, 待った秒数)。

    ロックは 2 段になっている。
    - ファイルのロック(flock): 話している、または聞いている間だけ持つ。プロセスが終わると外れる
    - 利用者の番: ファイルの中身に書く。利用者が「以上」を言わないまま聞き取りが時間切れになったとき、
      そのセッションが順番を持ち続ける。利用者が考えている間に、ほかのセッションが話しかけないため。
      同じセッションが聞き取りを再開しないまま FLOOR_HOLD_S 秒たつと、ほかのセッションも話せる
    - 「以上」で話し終えたあと(Claude が作業している間)は、早い者勝ちで、ほかのセッションも話せる

    wait_s 秒待っても取れなければ (None, False, 待った秒数) を返す。
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
        if time.time() - start >= wait_s or _cancelled.is_set():
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
        session_set_state(name, "away")  # 席を外した扱い。次に聞き取りを始めるまで、ターンの終わりを引き止めない
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


def project_dir() -> str:
    """Claude Code を始めたフォルダ。コマンドの途中で別のフォルダへ移っていても、変わらない。

    シェルの作業フォルダを使うと、下の階層へ移ったときに、セッションの名前が変わってしまう。
    Claude Code が渡す環境変数から調べ、分からなければ、いまの作業フォルダを使う。
    """
    d = os.environ.get("CLAUDE_PROJECT_DIR")
    if d:
        return d
    pid = os.environ.get("CLAUDE_PID", "")
    if pid.isdigit():
        try:
            out = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if line.startswith("n/"):
                    return line[1:]
        except (OSError, subprocess.SubprocessError):
            pass
    return os.getcwd()


def session_name(path: str | None = None) -> str:
    """Claude Code を始めたフォルダの名前から、先頭の番号と記号を除いたもの(例: 10_設計 → 設計)。"""
    base = os.path.basename((path or project_dir()).rstrip("/"))
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
        cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        _cg.append(cg)
    cg = _cg[0]
    # 0 は、すべての入力元を合わせた状態。キーの状態か、修飾キーのビットのどちらかで判定する
    return bool(cg.CGEventSourceKeyState(0, TALK_KEY)) or bool(cg.CGEventSourceFlagsState(0) & TALK_KEY_FLAG)


KEY_SETTLE_S = 0.15   # キーが、この秒数、単独で押され続けたら「話すために押した」とみなす
BARGE_SETTLE_S = 0.5  # 読み上げ中は、長めに待つ。⌘ と数字のようなショートカットで、読み上げを止めないため
_hold = {"since": 0.0, "chord": False, "settle": KEY_SETTLE_S}


def talk_held() -> bool:
    """話すためのキーが、単独で押されているか。

    キーを押している間に、ほかのキーが押されたら、ショートカット(⌘ と数字でアプリを切り替える、など)
    なので、話すための操作として扱わない。押した直後の一瞬も、ショートカットの途中かもしれないので待つ。
    """
    if not key_down():
        _hold["since"], _hold["chord"] = 0.0, False
        return False
    now = time.time()
    if not _hold["since"]:
        _hold["since"] = now
    if not _hold["chord"]:
        # 10 は、文字や数字などのキーが押された、という種類の出来事。修飾キーだけの操作は含まれない
        since_other_key = _cg[0].CGEventSourceSecondsSinceLastEventType(0, 10)
        if since_other_key < now - _hold["since"] + 0.03:
            _hold["chord"] = True
    if _hold["chord"]:
        return False
    return now - _hold["since"] >= _hold["settle"]


def input_device():
    """聞き取りに使うマイク。設定が builtin なら、Mac 本体のマイクの番号。それ以外は None(既定の入力)。

    Bluetooth のイヤホンのマイクは、通話用の粗い音(16kHz)で、開くたびにイヤホンが通話用の接続に切り替わる。
    Mac 本体のマイクを使うと、音がきれいで、切り替えの待ちも、読み上げの音質の低下もない。
    読み上げは、どちらの場合も、既定の出力(イヤホン)から出る。
    """
    rec = read_record() or {}
    if rec.get("mic") != "builtin":
        return None
    try:
        import sounddevice as sd

        for i, d in enumerate(sd.query_devices()):
            name = d.get("name", "")
            if d.get("max_input_channels", 0) > 0 and ("MacBook" in name or "Built-in" in name or "内蔵" in name):
                return i
    except Exception:
        pass
    return None  # 見つからなければ、既定の入力を使う


def mic_closed() -> None:
    """マイクを閉じた時刻を残す(どのセッションの読み上げも、これを見て少し待つ)。"""
    try:
        with open(MIC_CLOSED_PATH, "w", encoding="utf-8") as f:
            f.write(f"{time.time():.2f}")
        os.chmod(MIC_CLOSED_PATH, 0o600)
    except OSError:
        pass


def wait_mic_settle(cancel: threading.Event | None = None) -> None:
    """最後にマイクを閉じてから MIC_SETTLE_S たつまで待つ。"""
    try:
        with open(MIC_CLOSED_PATH, encoding="utf-8") as f:
            closed = float(f.read().strip() or 0)
    except (OSError, ValueError):
        return
    while 0 <= time.time() - closed < MIC_SETTLE_S:
        if cancel is not None and cancel.is_set():
            return
        time.sleep(0.05)


def play(path: str) -> None:
    try:
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def boost(pcm: bytes) -> bytes:
    """声が小さいとき(Mac 本体のマイクで、離れて話したときなど)、認識に渡す前に音を大きくする。"""
    samples = array.array("h", pcm)
    peak = max((abs(v) for v in samples), default=0)
    if not 300 <= peak < 12000:
        return pcm  # ほぼ無音か、十分な大きさ
    gain = min(8.0, 20000 / peak)
    return array.array("h", (max(-32768, min(32767, int(v * gain))) for v in samples)).tobytes()


_back: list = []


def to_notation(text: str) -> str:
    """利用者の読みがなの表にある語が、読み(カタカナ)で認識されたら、表記に直す(ワイファイ → Wi-Fi)。

    利用者が自分で登録した語だけを対象にする。共通の表の語(一般の語)は、直さない。
    """
    if not _back:
        pairs = []
        try:
            with open(YOMI_USER_PATH, encoding="utf-8") as f:
                for line in f:
                    if not line.startswith("#") and "\t" in line:
                        cols = [x.strip() for x in line.rstrip("\n").split("\t")]
                        word, reading = cols[0], cols[1]
                        if word and len(reading) >= 3:  # 短い読みは、ほかの語の一部に当たりやすいので直さない
                            pairs.append((reading, word))
                        # 3 列目からは、その語の聞き間違い(例: PostgreSQL が「ポストグレス」と認識される)。表記に直す
                        pairs.extend((wrong, word) for wrong in cols[2:] if word and len(wrong) >= 2)
        except OSError:
            pass
        _back.append(sorted(pairs, key=lambda x: -len(x[0])))
        # 表記のゆれ(大文字と小文字、ハイフンや空白の有無)も、登録した表記にそろえる(wifi → Wi-Fi)
        loose = []
        for word in dict.fromkeys(w for _, w in pairs):
            core = re.sub(r"[^A-Za-z0-9]", "", word)
            if len(core) >= 3 and core.isascii():
                loose.append((re.compile(r"(?<![A-Za-z0-9])" + r"[-\s]?".join(map(re.escape, core)) + r"(?![A-Za-z0-9])",
                                         re.IGNORECASE), word))
        _back.append(loose)
    for reading, word in _back[0]:
        if reading in text:
            # カタカナの語の一部には当てはめない(「イント」を登録していても、「ポイント」は直さない)
            text = re.sub(rf"(?<![ァ-ヶー]){re.escape(reading)}(?![ァ-ヶー])", lambda m: word, text)
    for pattern, word in _back[1]:
        text = pattern.sub(lambda m: word, text)
    return text


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        pad = b"\x00" * (RATE * 2 * 3 // 10)  # 前後に 0.3 秒の無音を足す。頭の音が欠けて認識されるのを防ぐ
        w.writeframes(pad + pcm + pad)
    return buf.getvalue()


# 無音や雑音を、音声認識が決まった言い回しにしてしまうことがある(動画の締めの挨拶など)。
# 区切りの全体がこれらに一致したら、聞こえなかったものとして捨てる
HALLUCINATIONS = (
    "ご視聴ありがとうございました", "ご視聴ありがとうございます", "ご清聴ありがとうございました",
    "最後までご視聴いただきありがとうございました", "最後までご視聴ありがとうございました",
    "チャンネル登録お願いします", "チャンネル登録をお願いします", "おやすみなさい", "字幕視聴ありがとうございました",
)
PROMPT_MAX_WORDS = 60   # 音声認識に教える語の上限(教えすぎると、効き目が落ちる)
_prompt: list[str] = []


def whisper_prompt() -> str:
    """音声認識に、あらかじめ教える語の並び。固有名詞の聞き間違いを減らす。

    教えるのは、利用者の読みがなの表の表記、音声で会話中のセッションの名前、共通の読みがなの表の表記。
    利用者の語を先に並べる。Mac 内の Whisper に渡すだけで、外へは出ない。
    """
    if not _prompt:
        words: list[str] = []

        def take(path: str) -> None:
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if not line.startswith("#") and "\t" in line:
                            w = line.split("\t")[0].strip()
                            if w and w not in words:
                                words.append(w)
            except OSError:
                pass

        take(YOMI_USER_PATH)
        for name in ((read_record() or {}).get("sessions") or {}):
            if name not in words:
                words.append(name)
        take(YOMI_PATH)
        _prompt.append("、".join(words[:PROMPT_MAX_WORDS]) + "。" if words else "")
    return _prompt[0]


def transcribe(client: httpx.Client, pcm: bytes) -> str:
    data = {"model": "whisper-1", "language": "ja", "response_format": "json"}
    prompt = whisper_prompt()
    if prompt:
        data["prompt"] = prompt
    r = client.post(WHISPER_URL, files={"file": ("segment.wav", to_wav(boost(pcm)), "audio/wav")}, data=data)
    r.raise_for_status()
    text = " ".join(r.json().get("text", "").split())
    bare = re.sub(rf"[{BOUNDARY}]", "", text)
    if bare in HALLUCINATIONS or (prompt and bare == re.sub(rf"[{BOUNDARY}]", "", prompt)):
        return ""  # 決まった聞き間違い、または、教えた語の並びがそのまま返ってきた
    return to_notation(text)


# 読みがな。字幕にはふつうの表記で出し、声では読みで読む。
# 1) 文の中の {表記|読み} は、字幕に「表記」、声に「読み」を使う
# 2) それ以外の英字の語は、読みがなの表(スクリプトと同じ場所の yomi.tsv、利用者の YOMI_USER_PATH)で読みに直す
YOMI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yomi.tsv")
YOMI_USER_PATH = os.path.expanduser("~/.voicemode/10-voice-yomi.tsv")
RUBY = re.compile(r"\{([^|{}]+)\|([^|{}]+)\}")


def load_yomi() -> dict[str, str]:
    """読みがなの表を読む。「小文字にした表記 → 読み」を返す。"""
    table: dict[str, str] = {}
    for path in (YOMI_PATH, YOMI_USER_PATH):  # あとから読むほう(利用者の表)が優先
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#") or "\t" not in line:
                        continue
                    word, reading = (x.strip() for x in line.rstrip("\n").split("\t")[:2])
                    if word and reading:
                        table[word.lower()] = reading
        except OSError:
            pass
    return table


KANJI_DIGITS = "〇一二三四五六七八九"
NUMBER = re.compile(r"(?<![A-Za-z0-9.])(\d+)(?:\.(\d+))?(?![A-Za-z0-9])( ?)")


def kanji_number(digits: str) -> str:
    """算用数字を、漢数字に直す(16 → 十六、2026 → 二千二十六)。日本語の声は、算用数字の読みが不安定なため。"""
    if len(digits) > 8 or (len(digits) > 1 and digits[0] == "0"):
        return "".join(KANJI_DIGITS[int(d)] for d in digits)  # 長い番号や、0 で始まる番号は、1 桁ずつ読む
    n = int(digits)
    if n == 0:
        return "〇"
    out = ""
    for unit, name in ((10000, "万"), (1, "")):
        part, n = divmod(n, unit)
        if part == 0:
            continue
        chunk = ""
        for u, nm in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
            d, part = divmod(part, u)
            if d:
                chunk += ("" if d == 1 and u > 1 else KANJI_DIGITS[d]) + nm
        out += chunk + name
    return out


def spoken_number(m: re.Match) -> str:
    """文の中の数を、声で読む形に直す。「2 つ」は「二つ」、「0.8 秒」は「れいてん八秒」。数のあとの空白は詰める。"""
    whole = kanji_number(m.group(1))
    if m.group(2):
        # 小数。「・」でつなぐと、声は点を飛ばして読む(1.25 が「いちにご」になった)ので、「てん」と書く
        whole = ("れい" if whole == "〇" else whole) + "てん" + "".join(KANJI_DIGITS[int(d)] for d in m.group(2))
    return whole


def split_say(text: str) -> tuple[str, str, list[tuple[int, int]]]:
    """読み上げる文を、字幕に出す文と、声で読む文に分ける。

    戻り値は (字幕の文, 声の文, 対応)。対応は、(声の文の文字位置, 字幕の文の文字位置) の並びで、
    表記と読みの長さが違っても、声の位置から字幕の位置を求められるようにする
    (「AWS」は 3 文字、「エーダブリューエス」は 9 文字)。
    """
    table = load_yomi()
    words = sorted(table, key=len, reverse=True)  # 長い表記を先に当てはめる
    # 英数字の語の一部(例: 「AIR」の中の「AI」)には当てはめない
    by_table = re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])", re.IGNORECASE
    ) if words else None
    shown: list[str] = []
    spoken: list[str] = []
    points = [(0, 0)]

    def add(shown_piece: str, spoken_piece: str) -> None:
        shown.append(shown_piece)
        spoken.append(spoken_piece)
        points.append((sum(map(len, spoken)), sum(map(len, shown))))

    def numbers(chunk: str) -> None:
        # 表にない部分。算用数字を、漢数字に直して読む(字幕には、算用数字のまま出す)
        pos = 0
        for m in NUMBER.finditer(chunk):
            if m.start() > pos:
                add(chunk[pos : m.start()], chunk[pos : m.start()])
            add(m.group(0), spoken_number(m))
            pos = m.end()
        if pos < len(chunk):
            add(chunk[pos:], chunk[pos:])

    def plain(chunk: str) -> None:
        pos = 0
        for m in (by_table.finditer(chunk) if by_table else ()):
            if m.start() > pos:
                numbers(chunk[pos : m.start()])
            add(m.group(0), table[m.group(0).lower()])
            pos = m.end()
        if pos < len(chunk):
            numbers(chunk[pos:])

    pos = 0
    for m in RUBY.finditer(text):  # {表記|読み} の読みは、表で置き換えない
        plain(text[pos : m.start()])
        add(m.group(1), m.group(2))
        pos = m.end()
    plain(text[pos:])
    return "".join(shown), "".join(spoken), points


def to_shown(points: list[tuple[int, int]], n: int) -> int:
    """声の文の n 文字目が、字幕の文の何文字目に当たるかを、対応から求める。"""
    for (s0, d0), (s1, d1) in zip(points, points[1:]):
        if n <= s1:
            return d0 + round((d1 - d0) * (n - s0) / max(1, s1 - s0))
    return points[-1][1]


def yomi_add(word: str, reading: str, wrongs: list[str] | None = None) -> int:
    """利用者の読みがなの表に、表記と読みを足す。同じ表記(大文字と小文字は区別しない)があれば書き換える。

    wrongs は、その語の聞き間違い(音声認識が出してしまう文字)。認識の結果に出てきたら、表記に直す。
    すでに登録されている聞き間違いは、引き継ぐ。
    """
    word, reading = " ".join(word.split()), " ".join(reading.split())
    wrongs = [" ".join(w.split()) for w in (wrongs or []) if w.strip()]
    if not word or not reading or "\t" in word + reading + "".join(wrongs):
        print("エラー: 表記と読みを指定してください", file=sys.stderr)
        return 2
    fd = os.open(YOMI_USER_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        lines = [line.rstrip("\n") for line in f]
        same = [line for line in lines if not line.startswith("#") and line.split("\t")[0].strip().lower() == word.lower()]
        for line in same:  # 前に登録した聞き間違いを引き継ぐ
            wrongs = [w.strip() for w in line.split("\t")[2:] if w.strip() and w.strip() not in wrongs] + wrongs
        kept = [line for line in lines if line not in same]
        kept.append("\t".join([word, reading] + wrongs))
        f.seek(0)
        f.truncate()
        f.write("\n".join(kept) + "\n")
    extra = f"。聞き間違い「{'」「'.join(wrongs)}」は {word} に直します" if wrongs else ""
    print(f"読みがなを登録しました: {word} → {reading}{extra}(全セッションで、次の読み上げから効きます)")
    return 0


def record_set(key: str, value) -> None:
    """セッションの記録に、全セッション共通の設定を 1 つ書く。"""
    fd = os.open(SESSIONS_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = json.loads(f.read() or "{}")
        except ValueError:
            data = {}
        data[key] = value
        f.seek(0)
        f.truncate()
        f.write(json.dumps(data, ensure_ascii=False))


def read_record() -> dict | None:
    """セッションの記録を読む。読めなければ None。

    書く側(sessions_update、字幕の窓)は、ロックを取って、中身を消してから書き直す。
    ロックなしで読むと、書いている最中の空の中身を読んでしまうので、読む側もロックを取る。
    """
    try:
        with open(SESSIONS_PATH, encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def listen_mode(default: str = "key") -> str:
    """字幕の窓で選ばれている、聞き取りの方式(key または word)。記録が読めなければ default。"""
    rec = read_record()
    if rec is None:
        return default
    return "word" if rec.get("mode") == "word" else "key"


def speech_speed(default: float = 1.0) -> float:
    """字幕の窓で選ばれている、読み上げの速さ。記録が読めなければ default。"""
    rec = read_record()
    if rec is None:
        return default
    try:
        speed = float(rec.get("speed", 1.0))
    except (ValueError, TypeError):
        return 1.0
    return speed if 0.5 <= speed <= 2.0 else 1.0


class Speaker:
    """Mac 内の Kokoro で音声を作り、途中で止められる形で再生する。

    字幕の窓のつまみで、再生の位置を変えられる。読み終えたあとも、聞き取りが続いている間は、
    つまみを動かすと、その位置から読み直す。位置は PLAYBACK_PATH に書き、字幕の窓が表示する。
    """

    def __init__(self, text: str, name: str = "", stop_on_key: bool = False,
                 shown: str | None = None, points: list[tuple[int, int]] | None = None):
        self.text = text                 # 声で読む文(読みがなに直したもの。名乗りを含む)
        self.shown = text if shown is None else shown          # 字幕に出ている文
        self.points = points or [(0, 0), (len(text), len(self.shown))]  # 声の文と字幕の文の、位置の対応
        self.name = name
        self.stop_on_key = stop_on_key   # 読み直しの途中でキーが押されたら止める(キーの方式)
        self.ready = threading.Event()   # 音声ができた(または失敗した)
        self.done = threading.Event()    # 最初の再生が終わった(または止めた、失敗した)
        self.cancel = threading.Event()  # 最初の再生を止める(割り込みなど)
        self.closed = threading.Event()  # 聞き取りが終わった。読み直しも受け付けない
        self.error: str | None = None
        self.pcm = b""
        self.written = 0  # 再生に渡したバイト数
        self.speed = speech_speed()
        self.seek_seen = self._seek_stamp()
        self.first_heard: str | None = None  # 最初の再生で伝わった範囲(読み直しでは変えない)
        self.pending: int | None = None      # 止められた所から読み上げを続ける、という指示(バイト位置)
        self.playing = False                 # いま、音を出しているか
        self.thread: threading.Thread | None = None
        threading.Thread(target=self._synth, daemon=True).start()

    def _request(self, text: str, speed: float) -> bytes:
        r = httpx.post(
            KOKORO_URL,
            json={"model": "tts-1", "voice": KOKORO_VOICE, "input": text, "response_format": "pcm", "speed": speed},
            timeout=120,
        )
        r.raise_for_status()
        return r.content

    def _synth(self) -> None:
        try:
            self.pcm = self._request(self.text, self.speed)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        self.ready.set()

    def heard_text(self) -> str:
        """止めた時点までに読み上げた部分を、再生した長さの割合から見積もる。

        見積もりなので、直前の文の切れ目まで戻して、控えめに返す。
        """
        if self.first_heard is not None:
            return self.first_heard
        if not self.pcm:
            return ""
        played = max(0, self.written - int(0.3 * KOKORO_RATE) * 2)  # 出力の遅れの分を引く
        n = int(len(self.text) * played / len(self.pcm))
        head = self.shown[: to_shown(self.points, n)]  # 字幕に出した文(Claude が書いた表記)で返す
        cut_at = max(head.rfind(c) for c in "。、？！")
        return head[: cut_at + 1] if cut_at >= 0 else ""

    def start(self) -> None:
        self.thread = threading.Thread(target=self._play, daemon=True)
        self.thread.start()

    def cut_short(self) -> bool:
        """読み上げが、途中で止められたままか。"""
        return bool(self.pcm) and self.done.is_set() and self.written < len(self.pcm)

    def resume(self) -> None:
        """止められた所の少し手前から、読み上げを続ける(キーに触れただけで、何も話されなかったとき)。"""
        self.first_heard = None
        self.pending = max(0, self.written - int(1.0 * KOKORO_RATE) * 2) // 2 * 2

    def close(self) -> None:
        """聞き取りの終わりに呼ぶ。再生を止め、つまみを使えない表示にする。"""
        self.cancel.set()
        self.closed.set()
        # 再生の後始末(音の出力を止めて閉じる)が終わるのを待つ。待たずにプログラムを終えると、
        # 後始末の最中に終了が重なり、異常終了することがあった
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        self._report(False, active=False)

    @staticmethod
    def _seek_stamp() -> float:
        try:
            return os.path.getmtime(SEEK_PATH)
        except OSError:
            return 0.0

    def _seek_request(self) -> float | None:
        """字幕の窓のつまみが動かされていたら、その位置(0〜1)を返す。"""
        stamp = self._seek_stamp()
        if stamp <= self.seek_seen:
            return None
        self.seek_seen = stamp
        try:
            with open(SEEK_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("name") not in ("", self.name):
                return None
            return min(1.0, max(0.0, float(data.get("frac", 0))))
        except (OSError, ValueError, TypeError):
            return None

    def _report(self, playing: bool, active: bool = True) -> None:
        """再生の位置を書く(字幕の窓のつまみが読む)。"""
        try:
            total = len(self.pcm) / (KOKORO_RATE * 2) if self.pcm else 0.0
            # 再生に渡してから音が出るまでの遅れ(約 0.3 秒)を引いて、聞こえている位置を書く。
            # map は、音の位置(0〜1)と字幕の文の位置(0〜1)の対応。字幕の色分けの位置を合わせるのに使う
            # (名乗りは字幕に出ない。表記と読みは長さが違う)
            heard = max(0, self.written - int(0.3 * KOKORO_RATE) * 2) if playing else self.written
            data = {"name": self.name, "pos": round(heard / (KOKORO_RATE * 2), 2), "dur": round(total, 2),
                    "map": [[round(a / max(1, len(self.text)), 4), round(b / max(1, len(self.shown)), 4)]
                            for a, b in self.points],
                    "playing": playing, "active": active, "t": round(time.time(), 2)}
            fd = os.open(PLAYBACK_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False))
            os.replace(PLAYBACK_PATH + ".tmp", PLAYBACK_PATH)
        except OSError:
            pass

    def _align(self, frac: float) -> int:
        """位置(0〜1)を、音声の中のバイト位置に直す(16bit の境目にそろえる)。"""
        return int(len(self.pcm) * frac) // 2 * 2

    def _seek_start(self, frac: float) -> int:
        """つまみで指定された位置から、読み直しを始めるバイト位置を決める。

        文字と音の対応は概算なので、指定より先から始まることがある。少し手前から始める。
        すぐ手前(12 文字以内)に文の区切り(。、？！)があれば、そこから始める。
        """
        n = int(len(self.text) * frac)
        cut_at = max(self.text.rfind(c, 0, n) for c in "。、？！")
        start = cut_at + 1 if cut_at >= 0 and n - (cut_at + 1) <= 12 else max(0, n - 3)
        back = int(0.2 * KOKORO_RATE) * 2  # 0.2 秒ぶん手前から
        return max(0, self._align(start / max(1, len(self.text))) - back)

    def _run(self, out, i: int, first: bool) -> None:
        """位置 i から最後まで再生する。止める指示、つまみ、速さの変更に、0.05 秒ごとに応じる。"""
        step = KOKORO_RATE // 20 * 2
        checked = 0.0
        self.playing = True
        try:
            self._run_loop(out, i, first, step, checked)
        finally:
            self.playing = False

    def _run_loop(self, out, i: int, first: bool, step: int, checked: float) -> None:
        while i < len(self.pcm):
            if self.closed.is_set() or (first and self.cancel.is_set()):
                out.abort()
                return
            if not first and self.stop_on_key and talk_held():
                out.abort()  # 読み直しの途中で、利用者が話し始めた
                return
            out.write(self.pcm[i : i + step])
            i += step
            self.written = min(i, len(self.pcm))
            if time.time() - checked >= 0.2:
                checked = time.time()
                self._report(True)
                frac = self._seek_request()
                if frac is not None:
                    # 右端まで動かされたら、そこで読み終える。それ以外は、手前の区切りから読み直す
                    i = self._align(1.0) if frac >= 0.995 else self._seek_start(frac)
                    self.written = min(i, len(self.pcm))
                speed = speech_speed(self.speed)  # 記録が読めないときは、いまの速さのまま
                if abs(speed - self.speed) > 0.01:
                    # 速さが変えられた。全体を新しい速さで作り直し、読み終えた文の切れ目から続ける
                    heard = len(self.heard_text()) / max(1, len(self.text))
                    self.speed = speed
                    try:
                        self.pcm = self._request(self.text, speed)
                        i = self._align(heard)
                    except Exception:
                        pass  # 作り直せなければ、いまの速さのまま読み続ける

    def _open(self, sd):
        """ほかの読み上げ(別のセッションの知らせなど)が終わるのを待ち、出力を開く。"""
        speak_lock = open(SPEAK_LOCK_PATH, "a+", encoding="utf-8")
        os.chmod(SPEAK_LOCK_PATH, 0o600)
        waited = 0.0
        while waited < 60:
            try:
                fcntl.flock(speak_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if self.cancel.is_set() or self.closed.is_set():
                    speak_lock.close()
                    return None, None
                time.sleep(0.2)
                waited += 0.2
        wait_mic_settle(self.cancel)
        return speak_lock, sd.RawOutputStream(samplerate=KOKORO_RATE, channels=1, dtype="int16")

    def _play(self) -> None:
        import sounddevice as sd

        try:
            self.ready.wait()
            if self.error or self.cancel.is_set():
                return
            speak_lock, out = self._open(sd)
            if out is None:
                return
            with speak_lock, out:
                self._run(out, 0, True)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.first_heard = self.heard_text()
            self.done.set()
        # 読み終えたあと。聞き取りが続いている間は、つまみが動かされたら、その位置から読み直す
        try:
            self._report(False)
            beat = time.time()
            while not self.closed.wait(0.2):
                if time.time() - beat >= 1:
                    beat = time.time()
                    self._report(False)  # つまみが使えることを、字幕の窓に知らせ続ける
                frac = self._seek_request()
                start, self.pending = self.pending, None
                if (frac is None and start is None) or not self.pcm:
                    continue
                self.cancel.clear()
                speak_lock, out = self._open(sd)
                if out is None:
                    continue
                self.written = start if frac is None else self._seek_start(frac)
                with speak_lock, out:
                    self._run(out, self.written, False)
                self._report(False)
        except Exception:
            pass
        finally:
            self._report(False, active=False)


def mic_frames(stop: threading.Event, on_ready):
    import sounddevice as sd

    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(bytes(indata))

    device = input_device()
    warmup = WARMUP_S if device is None else BUILTIN_WARMUP_S
    try:
        with sd.RawInputStream(device=device, samplerate=RATE, blocksize=FRAME_BYTES // 2, dtype="int16", channels=1, callback=callback):
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
                if not ready and received >= warmup * RATE * 2:
                    ready = True
                    on_ready()
                while len(buf) >= FRAME_BYTES:
                    yield buf[:FRAME_BYTES]
                    buf = buf[FRAME_BYTES:]
    finally:
        if device is None:
            mic_closed()  # イヤホンのマイクを閉じた。接続が戻るまで、読み上げを少し待たせる


def key_frames(stop: threading.Event, on_ready):
    """キーの方式の音声。キーが押されている間だけマイクを開く。

    30 ミリ秒ごとに 1 つ返す。マイクを閉じている間と、開いた直後(Bluetooth が音を拾えない間)は None。
    開いてから KEY_WARMUP_S たつと on_ready を呼び(合図を鳴らす)、そこからの音声を返す。
    キーを離して RELEASE_GRACE_S たつと、マイクを閉じる。
    """
    import sounddevice as sd

    while not stop.is_set():
        if not talk_held():
            time.sleep(FRAME_S)
            yield None
            continue
        q: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(bytes(indata))

        device = input_device()
        warmup = KEY_WARMUP_S if device is None else BUILTIN_WARMUP_S
        stream = sd.RawInputStream(device=device, samplerate=RATE, blocksize=FRAME_BYTES // 2, dtype="int16", channels=1, callback=callback)
        stream.start()
        try:
            buf = b""
            received = 0
            ready = False
            released = 0.0
            while not stop.is_set() and released < RELEASE_GRACE_S:
                try:
                    chunk = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                buf += chunk
                received += len(chunk)
                if not ready and received >= warmup * RATE * 2:
                    ready = True
                    buf = b""  # 合図より前の音は捨てる
                    on_ready()
                while len(buf) >= FRAME_BYTES:
                    frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                    released = 0.0 if talk_held() else released + FRAME_S
                    yield frame if ready else None
                if not ready and not talk_held():
                    break  # 合図の前に離した(ショートカットなど)。何もしない
        finally:
            # 聞き取りを途中でやめたときも、ここでマイクを閉じる
            stream.stop()
            stream.close()
            if device is None:
                mic_closed()


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
    停止ボタンが押されている、「あとで」に置かれている、ミュート中のときは、黙っている。
    利用者が別のセッションを話す相手に選んでいる間は、選び直されるまで待つ。
    待機中のセッションが話しかけるときと同じ順番待ちをする。ほかのセッションの読み上げと、
    それに続く利用者の発言が終わってから話す。マイクは開かず、利用者の番にも触れない。
    """
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    # hook には CLAUDE_PROJECT_DIR が渡る。なければ、JSON の作業フォルダを使う
    hook_cwd = data.get("cwd") if isinstance(data, dict) else None
    name = session_name(os.environ.get("CLAUDE_PROJECT_DIR") or hook_cwd)

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
        if me.get("state") == "muted" or (me.get("lane") == "later" and rec.get("focus") != name):
            return None
        return me.get("state"), me.get("t")

    def focus_of():
        """利用者が話す相手に選んでいるセッション。選んでいなければ None。"""
        try:
            with open(SESSIONS_PATH, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return None
        focus = rec.get("focus")
        return focus if isinstance(rec.get("sessions"), dict) and focus in rec["sessions"] else None

    before = current()
    if before is None:
        return 0
    text = f"{name}、確認待ちです。画面を見てください。"
    shown, spoken, points = split_say(text)  # セッションの名前も、読みがなの表で読みに直す
    speaker = Speaker(spoken, name, shown=shown, points=points)
    try:
        lock = open(LOCK_PATH, "a+", encoding="utf-8")
    except OSError:
        return 0
    start = time.time()
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            holder, last, active = read_floor(lock)
            others_turn = holder and holder != name and active and time.time() - last < FLOOR_HOLD_S
            if not others_turn and focus_of() in (None, name):
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
    speaker.close()
    time.sleep(0.4)  # つまみを使えない表示に戻すのを待つ
    if speaker.error:
        play(CUE_MISS)
        time.sleep(1)
    return 0


STOP_CHECK_RECENT_S = 1800  # 最後に声で話してから、この秒数を過ぎたセッションは、引き止めない


def stop_check() -> int:
    """Claude Code の hook(Stop)から呼ばれる。音声会話の途中で、聞き取りを呼ばずにターンを終えようとしたら、引き止める。

    標準入力の JSON を読み、引き止めるときは {"decision": "block", "reason": …} を出力する。
    引き止めるのは 1 回だけ。次のどれかに当てはまれば、何もしない(そのままターンを終えさせる)。
    - すでに引き止めたあと(stop_hook_active)
    - このセッションが、音声で会話していない(記録にない)。ミュート中、席を外した扱い(away)
    - 最後に声で話してから、STOP_CHECK_RECENT_S を過ぎている
    - 読み上げのサービスが動いていない
    - 聞き取りが動いている(バックグラウンドで順番を待っている場合を含む)
    """
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(data, dict) or data.get("stop_hook_active"):
        return 0
    name = session_name(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd"))
    rec = read_record() or {}
    me = (rec.get("sessions") or {}).get(name) if isinstance(rec.get("sessions"), dict) else None
    if rec.get("boot") != boot_id() or not me or me.get("state") in ("muted", "away"):
        return 0
    if time.time() - (me.get("t") or 0) > STOP_CHECK_RECENT_S:
        return 0
    if me.get("lpid") and pid_alive(me["lpid"]):
        return 0  # 聞き取りが動いている
    for task in data.get("background_tasks") or []:
        if isinstance(task, dict) and "listen" in str(task.get("command", "")) and "voice.sh" in str(task.get("command", "")):
            return 0  # バックグラウンドで、聞き取りが順番を待っている
    try:
        import socket

        socket.create_connection(("127.0.0.1", 8880), timeout=0.5).close()
    except OSError:
        return 0  # 読み上げのサービスが止まっている
    print(json.dumps({
        "decision": "block",
        "reason": "音声会話の途中です。聞き取りを呼ばずにターンを終えると、利用者の声が届かなくなります。"
                  "voice.sh listen --say '…' で聞き取りに戻ってください(利用者に操作を頼んだなら、完了の返事を聞き取る。"
                  "順番が来ないなら、--wait 21600 を付けてバックグラウンドで実行する)。"
                  "利用者が音声の終了を望んでいるなら、voice.sh end を実行してください。",
    }, ensure_ascii=False))
    return 0


def memo_watch() -> int:
    """Claude の作業中(どのセッションもマイクを開いていない間)に、キーを押して話したメモを預かる。

    voice.sh on が起動し、off が止める。キーが押されている間だけマイクを開く。
    宛先は、利用者が話す相手に選んでいるセッション。選んでいなければ、最後に話していたセッション。
    """
    single = open(MEMO_WATCH_LOCK, "a+", encoding="utf-8")
    try:
        fcntl.flock(single, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # すでに動いている
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def target(lock) -> str | None:
        """メモの宛先。停止中や、宛先のセッションがミュート中のときは None(マイクを開かない)。"""
        try:
            with open(SESSIONS_PATH, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return None
        sessions = rec.get("sessions") if isinstance(rec.get("sessions"), dict) else {}
        if rec.get("boot") != boot_id() or rec.get("paused") or rec.get("mode") == "word":
            return None  # 合言葉の方式では、キーを使わない
        name = rec.get("focus") if rec.get("focus") in sessions else read_floor(lock)[0]
        if name not in sessions or sessions[name].get("state") == "muted":
            return None
        return name

    def held_for(seconds: float) -> bool:
        start = time.time()
        while time.time() - start < seconds:
            if not talk_held():
                return False
            time.sleep(0.03)
        return True

    def wait_release() -> None:
        while key_down():
            time.sleep(0.1)

    while True:
        time.sleep(0.05)
        lock = None
        try:
            if not talk_held() or not held_for(MEMO_MIN_HOLD_S):
                continue
            lock = open(LOCK_PATH, "a+", encoding="utf-8")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                wait_release()  # どこかのセッションが話しているか、聞いている。そちらが受け取る
                continue
            name = target(lock)
            if not name:
                wait_release()
                continue
            # マイクを開く。Bluetooth は開いた直後の音を拾えないので、合図を鳴らしてから話してもらう
            stop = threading.Event()
            ready = threading.Event()

            def on_ready() -> None:
                ready.set()
                play(CUE_START)

            frames: list[bytes] = []
            released = 0.0
            for frame in mic_frames(stop, on_ready):
                if ready.is_set():
                    frames.append(frame)
                released = 0.0 if talk_held() else released + FRAME_S
                if released >= RELEASE_GRACE_S or len(frames) * FRAME_S >= 120:
                    stop.set()
            if not ready.is_set():
                continue  # 合図の前に離した(ショートカットなど)。何も鳴らさない
            pcm = b"".join(frames)
            text = ""
            if len(pcm) >= int(0.5 * RATE * 2):
                try:
                    with httpx.Client(timeout=120) as client:
                        text = transcribe(client, pcm)
                except Exception as e:  # Whisper に届かない。預かれなかったことを音で知らせる
                    print(f"メモの見張り: 文字にできませんでした({type(e).__name__}: {e})", file=sys.stderr)
            m = MEMO_PATTERN.match(text)
            if m and m.group(1).strip():
                text = m.group(1)
            if text.strip():
                memo_add(name, text)
                caption(name, "メモ", text)
                play(CUE_MEMO)
            else:
                play(CUE_MISS)
        except Exception as e:  # 見張りは止めない
            print(f"メモの見張り: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(1)
        finally:
            if lock is not None:
                lock.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="読み上げてから、合言葉が聞こえるまで聞き続け、文字にして出力する")
    ap.add_argument("--say", help="聞き取りの前に読み上げる文。読み上げ中に話すと割り込める")
    ap.add_argument("--no-reply", action="store_true", help="読み上げるだけで、聞き取らずに終わる。順番も手放す(返事のいらない一言に使う。voice.sh say が付ける)")
    ap.add_argument("--name", default=session_name(), help="このセッションの呼び名(既定: 作業フォルダの名前)")
    ap.add_argument("--mode", choices=("key", "word"), default=None, help="聞き取りの方式。key: キーを押している間だけ聞く。word: 合言葉が聞こえるまで聞く。指定しなければ、字幕の窓で選ばれている方式(選ばれていなければ key)。検証のとき以外は指定しない")
    ap.add_argument("--wait", type=float, default=LOCK_WAIT_S, help=f"話す順番を待つ秒数の上限(既定: {LOCK_WAIT_S})。バックグラウンドで長く待つときに大きくする")
    ap.add_argument("--end-word", default="以上", help="発言の終わりを示す合言葉(既定: 以上)")
    ap.add_argument("--idle", type=float, default=300, help="声が聞こえない状態がこの秒数続いたら打ち切る(既定: 300)")
    ap.add_argument("--max", type=float, default=480, help="聞き始めてからこの秒数で打ち切る(既定: 480)")
    ap.add_argument("--release", action="store_true", help="持ち続けている順番を手放して終わる(時間切れのあと、聞き取りを再開しないときに使う)")
    ap.add_argument("--end", action="store_true", help="音声での会話を終える。順番を手放し、字幕の窓のタブから消す")
    ap.add_argument("--yomi", nargs="+", metavar="語", help="利用者の読みがなの表に足す。表記、読み、(あれば)聞き間違いの順に指定する。同じ表記があれば書き換える")
    ap.add_argument("--mic", choices=("builtin", "default"), help="聞き取りに使うマイクを選ぶ。builtin: Mac 本体のマイク。default: 既定の入力(イヤホンなど)。全セッションで共通")
    ap.add_argument("--print-mode", action="store_true", help="いま選ばれている聞き取りの方式を表示する(voice.sh on と status が呼ぶ)")
    ap.add_argument("--unmute", action="store_true", help="このセッションのミュートを解く(voice.sh on が呼ぶ)")
    ap.add_argument("--cancel", action="store_true", help="このセッションの、動いている聞き取り(バックグラウンドで待っているものを含む)を止める")
    ap.add_argument("--stop-check", action="store_true", help="hook 用。音声会話の途中で、聞き取りを呼ばずにターンを終えようとしたら、引き止める")
    ap.add_argument("--notify", action="store_true", help="hook 用。標準入力の JSON にある作業フォルダのセッションが音声で会話中なら、確認待ちを声で知らせる")
    ap.add_argument("--memo-watch", action="store_true", help="Claude の作業中に、キーを押して話したメモを預かる見張りとして動く(voice.sh on が起動する)")
    ap.add_argument("--file", help="検証用。マイクの代わりに WAV ファイルを読む")
    ap.add_argument("--verbose", action="store_true", help="区切りごとの認識結果を標準エラーに出す")
    args = ap.parse_args()

    if args.notify:
        return notify()
    if args.stop_check:
        return stop_check()
    if args.cancel:
        me = ((read_record() or {}).get("sessions") or {}).get(args.name) or {}
        lpid = me.get("lpid")
        if lpid and lpid != os.getpid() and pid_alive(lpid):
            os.kill(int(lpid), signal.SIGTERM)
            print("聞き取りを止めました")
        else:
            print("動いている聞き取りは、ありません")
        return 0
    if args.memo_watch:
        return memo_watch()
    if args.yomi:
        if len(args.yomi) < 2:
            print("エラー: 表記と読みを指定してください", file=sys.stderr)
            return 2
        return yomi_add(args.yomi[0], args.yomi[1], args.yomi[2:])
    if args.mic:
        record_set("mic", args.mic)
    if args.print_mode or args.mic:
        print("方式: キーを押している間だけ聞く(右コマンド)" if listen_mode() == "key"
              else "方式: 「以上」と言うまで聞く(マイクは開いたまま)")
        if (read_record() or {}).get("mic") == "builtin":
            print("マイク: Mac 本体のマイク" if input_device() is not None
                  else "マイク: Mac 本体のマイクが見つからないので、既定の入力を使います")
        else:
            print("マイク: 既定の入力(イヤホンなど)")
        return 0
    if args.unmute:
        if sessions_update(args.name).get(args.name, {}).get("state") == "muted":
            session_set_state(args.name, "working")
        return 0
    if args.release or args.end:
        release_turn(args.name)
        if args.end:
            session_end(args.name)
        return 0

    # 方式は、全セッションで共通。字幕の窓のボタンで切り替える。聞き取りの途中で切り替えられたら、その場で従う
    follow_mode = args.mode is None and not args.file
    if args.mode is None:
        args.mode = listen_mode()
    key_mode = args.mode == "key" and not args.file
    key_note = ""
    if not args.file:
        try:
            key_down()
            if args.no_reply:
                # 返事のいらない一言は、方式によらず、マイクを開かずに読み上げる。止められるのはキーだけ
                key_mode, follow_mode = True, False
        except (OSError, AttributeError):
            follow_mode = False
            if key_mode:
                key_mode = False
                key_note = f"[キーの状態を調べられないため、「{args.end_word}」で終える方式で聞き取りました]"
    pattern = end_pattern(args.end_word)
    vad = webrtcvad.Vad(3)
    stop = threading.Event()
    texts: list[str] = []
    state: dict = {"reason": None, "error": None, "memo_hold": False, "memo_count": 0, "memo_parts": []}

    def memo_flush() -> None:
        """キーを押している間に話されたメモを、1 件にまとめて預かる(息つぎで区切れても、分けない)。"""
        parts, state["memo_parts"] = state["memo_parts"], []
        body = " ".join(parts)
        if body.strip():
            memo_add(args.name, body)
            caption(args.name, "メモ", body)
            state["memo_count"] += 1
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
                    if not key_mode and MUTE_PATTERN.match(text):
                        # ミュートは、合言葉の方式だけ。キーの方式では、押していない間はマイクが閉じているので要らない
                        state["reason"] = "mute"
                        stop.set()
                        continue
                    memo = MEMO_PATTERN.match(text)
                    if memo or state["memo_hold"]:
                        # メモは返事と分けて預かる。キーの方式では、離すまでの続きもメモにする
                        body = memo.group(1) if memo else text
                        state["memo_hold"] = key_mode
                        ended = pattern.search(body)
                        if ended:
                            body = body[: ended.start()]  # 「以上」はメモに入れない
                        if body.strip():
                            state["memo_parts"].append(body)
                        if not key_mode:
                            memo_flush()  # 合言葉の方式では、区切りごとに預かる
                        if ended and not key_mode:
                            # 合言葉の方式では、メモのあとの「以上」でも話し終わりにする
                            state["reason"] = "end-word"
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
        _cancelled.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    from_file = bool(args.file)
    lock = None
    shown_say, say, points = split_say(args.say) if args.say else (None, None, [])
    if not from_file:
        if sessions_update(args.name).get(args.name, {}).get("state") == "muted":
            # 利用者がミュートにしている(聞き取り中、または作業中に「ミュート」と言った)。話しかけない
            print("[ミュート: 利用者がミュートにしています。読み上げていません。声で呼びかけず、画面に「再開するときは /10-voice と入力してください」と書いて待ってください。利用者が再開を頼んだら、voice.sh on を実行してから聞き取りを始めます]")
            return 0
        lock, switched, waited = acquire_turn(args.name, args.wait)
        if lock is None and _cancelled.is_set():
            sessions_update(args.name, "working")
            print("[中断されました]")
            return 0
        if lock is None:
            print("[順番待ち: まだ話せません(ほかのセッションが会話中、利用者が別のセッションを話す相手に選んでいる、このセッションが「あとで」に置かれている、停止ボタンが押されている、のいずれか)。読み上げていません]")
            return 0
        if args.wait <= LOCK_WAIT_S:  # 長く待つ指定(バックグラウンド)のときは、コマンドの制限時間を気にしない
            args.max = max(MIN_LISTEN_S, min(args.max, TOTAL_LIMIT_S - waited))
        if switched and say:
            # 別のセッションから切り替わったときは名乗る。字幕には出さない。
            # セッションの名前も、読みがなの表で読みに直す(英字の名前を、登録した読みで読む)
            lead = f"{split_say(args.name)[1]}、からです。"
            say = lead + say
            points = [(0, 0)] + [(a + len(lead), b) for a, b in points]
    speaker = (Speaker(say, args.name, stop_on_key=key_mode, shown=shown_say, points=points)
               if say and not from_file else None)

    mode_box = {"key": key_mode}   # 音声の取り込み(下の frames)に、いまの方式を伝える
    started = {"speaker": False}

    def start_speaker() -> None:
        if speaker and not started["speaker"]:
            started["speaker"] = True
            caption(args.name, "Claude", shown_say)  # 字幕には、ふつうの表記で出す
            speaker.start()

    def on_ready() -> None:
        # 合言葉の方式で、マイクが開いた。読み上げがまだなら始める。読み上げ中でなければ、合図を鳴らす
        fresh = bool(speaker) and not started["speaker"]
        start_speaker()
        if not fresh and not (speaker and not speaker.done.is_set()):
            play(CUE_START)

    def live_frames():
        """いまの方式で音声を取り込む。方式が切り替えられたら、取り込み方を入れ替える。"""
        while not stop.is_set():
            mine = mode_box["key"]
            inner = threading.Event()
            source = key_frames(inner, lambda: play(CUE_START)) if mine else mic_frames(inner, on_ready)
            try:
                for frame in source:
                    yield frame
                    if stop.is_set() or mode_box["key"] != mine:
                        break
            finally:
                inner.set()
                source.close()

    if from_file:
        frames = file_frames(args.file, stop)
    else:
        if key_mode:
            start_speaker()  # キーの方式では、マイクを開かずに読み上げを始める。マイクは、キーが押されたときだけ開く
        frames = live_frames()

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
    memo_seen = 0     # 合図を鳴らし終えたメモの数
    up_ticks = 0      # キーが離されてからの回数(約 30 ミリ秒ごと)
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
            # 読み上げ中は、キーを長めに押したときだけ、話すための操作とみなす
            _hold["settle"] = BARGE_SETTLE_S if speaker and speaker.playing else KEY_SETTLE_S
            down = talk_held()
            if frame is not None:
                # マイクが開き、合図が鳴ったあとの音声。ここから録る(声の判定を待つと、話し始めが欠ける)
                if not talking:
                    talking = True
                    in_speech = True
                    segment = []
                    voiced_frames = 0
                    silent_run = 0
                    preroll.clear()
                    hold_frames = 0
                hold_frames += 1
            if down:
                release_run = 0
            elif talking:
                release_run += 1
                if release_run * FRAME_S >= RELEASE_GRACE_S:
                    # キーを離した。話していれば発言の終わり。何も話していなければ、聞き取りを続ける
                    talking = False
                    if in_speech:
                        cut()
                    jobs.join()
                    state["memo_hold"] = False
                    memo_flush()
                    if state["reason"]:
                        break
                    if texts:
                        state["reason"] = "released"
                        break
                    if state["memo_count"] > memo_seen:
                        memo_seen = state["memo_count"]
                        play(CUE_MEMO)  # メモを預かった。聞き取りは続ける
                    elif hold_frames * FRAME_S >= KEY_MIN_HOLD_S:
                        play(CUE_MISS)  # 押して話したのに、文字にならなかった。もう一度話してもらう
        # キーの方式では、マイクを閉じている間は音声がない(frame は None)
        voiced = vad.is_speech(frame, RATE) if frame is not None else False
        recent.append(voiced)
        if args.verbose and frame is not None:
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
                if args.no_reply and not barged:
                    # 返事のいらない一言。聞き取らずに終わり、順番を手放す(ほかのセッションが、すぐ話せる)。
                    # 利用者が途中でキーを押して止めたときは、言いたいことがあるので、ふつうに聞き取る
                    state["reason"] = state["reason"] or "said"
                    break
                if not barged and not key_mode:
                    play(CUE_START)  # キーの方式では鳴らさない。合図は、キーを押してマイクが開いたときに鳴る
            elif not barged and (down if key_mode else in_speech and voiced_frames * FRAME_S >= BARGE_VOICED_S):
                barged = True
                speaker.cancel.set()

        up_ticks = 0 if down else up_ticks + 1
        if (key_mode and barged and speaker and not talking and not in_speech
                and up_ticks * FRAME_S >= RELEASE_GRACE_S + 0.3   # マイクが閉じるのを待つ。閉じてから、接続が戻るまでは、読み上げの側が待つ
                and not texts and state["memo_count"] == memo_seen and speaker.cut_short()):
            # キーで読み上げを止めたが、何も話されなかった(触れただけ、ショートカットの途中だった、聞き取れなかった)。
            # 黙って待ち続けないように、止めた所から読み上げを続ける
            barged = False
            speaker.resume()

        if frame is None:
            pass
        elif in_speech:
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
            if follow_mode and (listen_mode("key" if key_mode else "word") == "key") != key_mode and not talking:
                # 字幕の窓で、方式が切り替えられた。その場で従う。話している途中の分は、ここまでで文字にする
                if in_speech:
                    cut()
                key_mode = not key_mode
                mode_box["key"] = key_mode
                if speaker:
                    speaker.stop_on_key = key_mode
                in_speech, release_run, since_voice = False, 0, 0.0
                recent.clear()
                preroll.clear()
                state["memo_hold"] = False
                if key_mode:
                    start_speaker()  # 読み上げがまだ始まっていなければ、始める(合言葉の方式では、マイクが開くのを待っていた)
            if _flags["focus"] and _flags["focus"] != args.name and not talking:
                # 利用者が、別のセッションを話す相手に選んだ。読み上げも聞き取りもやめて、順番をゆずる
                state["reason"] = state["reason"] or "yield"
                break
        if since_voice >= args.idle and not in_speech and not talking:
            jobs.join()
            state["reason"] = state["reason"] or "idle"
            break
        if elapsed >= args.max:
            state["reason"] = state["reason"] or "max"
            break

    stop.set()
    frames.close()  # ここでマイクを閉じる(文字にするのを待つ間、開いたままにしない)
    cut_short = bool(speaker) and speaking and not barged  # 読み上げの途中で、停止や保留になった
    if speaker:
        speaker.close()
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
        # 保留や停止で止まったセッションは、まだ話したいことがある。作業中(灰色)ではなく、順番待ち(橙)にする
        after = "muted" if reason == "mute" else "waiting" if reason in ("yield", "paused") else "working"
        sessions_update(args.name, after)
    if not from_file:
        if reason != "said":
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
    if cut_short and reason in ("yield", "paused"):
        heard = speaker.heard_text() if speaker else ""
        print(f"[読み上げは途中で止まりました。伝わったのは、およそ次の部分までです:「{heard}」]" if heard
              else "[読み上げは、始めてすぐに止まりました。ほぼ伝わっていません]")
    if texts:
        print("\n".join(texts))
    memo_flush()
    if not from_file and reason != "interrupted":
        # 中断されたときは、出力が読まれないので、預かったまま残す(次の聞き取りで渡す)
        for m in memo_take(args.name):
            print(f"[メモ: {m}]")
    if key_note:
        print(key_note)
    waiting_for = "キーは押されていません" if key_mode else f"「{args.end_word}」は聞こえていません"
    idle_what = "キーが押されませんでした" if key_mode else "声が聞こえませんでした"
    notes = {
        "idle": f"[打ち切り: {args.idle:g} 秒間、{idle_what}。{waiting_for}]",
        "max": f"[打ち切り: 制限時間({args.max:g} 秒)に達しました。{'発言の途中かもしれません' if key_mode else waiting_for}]",
        "mute": "[ミュート: 利用者が「ミュート」と言いました。マイクを閉じました]",
        "paused": "[一時停止: 利用者が字幕の窓の停止ボタンを押しました。マイクを閉じました。再開ボタンが押されるまで、どのセッションも話しません]",
        "yield": f"[保留: 利用者が字幕の窓で、別のセッション({_flags['focus']})を話す相手に選びました。読み上げと聞き取りをやめました。利用者が選び直すまで、このセッションは話せません]",
        "interrupted": "[中断されました]",
        "eof": f"[音声ファイルの終わりに達しました。「{args.end_word}」は聞こえていません]",
    }
    if reason in notes:
        print(notes[reason])
    return 0


if __name__ == "__main__":
    sys.exit(main())
