"""読み上げてから、合言葉(既定は「以上」)が聞こえるまで、マイクを閉じずに聞き続ける。

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
import fcntl
import io
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

# Bluetooth のイヤホンは、マイクを開いた直後に通話用の接続へ切り替わり、
# その間の約 1 秒は音が出ない。音声が届き始めてからこの秒数を待って音を出す
WARMUP_S = 1.5
CUE_START = "/System/Library/Sounds/Ping.aiff"
CUE_END = "/System/Library/Sounds/Glass.aiff"

# 複数のセッションで同時に使うときの順番待ち。ロックを持つセッションだけが話して聞く。
# ロックはプロセスが終わると自動で外れる。ファイルには、最後に使ったセッションの名前だけを書く
LOCK_PATH = os.path.expanduser("~/.voicemode/10-voice.lock")
LOCK_WAIT_S = 90

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


def acquire_turn(name: str):
    """順番を取る。戻り値は (ロックのファイル, 直前に使ったのが別のセッションか)。

    LOCK_WAIT_S 待っても取れなければ (None, False) を返す。
    """
    f = open(LOCK_PATH, "a+", encoding="utf-8")
    os.chmod(LOCK_PATH, 0o600)
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                f.close()
                return None, False
            time.sleep(0.2)
    f.seek(0)
    last = f.read().strip()
    f.seek(0)
    f.truncate()
    f.write(name)
    f.flush()
    return f, bool(last) and last != name


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


def session_name() -> str:
    """作業フォルダの名前から、先頭の番号と記号を除いたもの(例: 10_設計 → 設計)。"""
    base = os.path.basename(os.getcwd())
    return re.sub(r"^[\d_\-\s]+", "", base) or base


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
        w.writeframes(pcm)
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
            with sd.RawOutputStream(samplerate=KOKORO_RATE, channels=1, dtype="int16") as out:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="読み上げてから、合言葉が聞こえるまで聞き続け、文字にして出力する")
    ap.add_argument("--say", help="聞き取りの前に読み上げる文。読み上げ中に話すと割り込める")
    ap.add_argument("--name", default=session_name(), help="このセッションの呼び名(既定: 作業フォルダの名前)")
    ap.add_argument("--end-word", default="以上", help="発言の終わりを示す合言葉(既定: 以上)")
    ap.add_argument("--idle", type=float, default=60, help="声が聞こえない状態がこの秒数続いたら打ち切る(既定: 60)")
    ap.add_argument("--max", type=float, default=480, help="聞き始めてからこの秒数で打ち切る(既定: 480)")
    ap.add_argument("--file", help="検証用。マイクの代わりに WAV ファイルを読む")
    ap.add_argument("--verbose", action="store_true", help="区切りごとの認識結果を標準エラーに出す")
    args = ap.parse_args()

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
        lock, switched = acquire_turn(args.name)
        if lock is None:
            print(f"[順番待ち: ほかのセッションが音声で会話中のため、{LOCK_WAIT_S} 秒待っても始められませんでした。読み上げていません]")
            return 0
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

    def cut() -> None:
        nonlocal segment, in_speech, voiced_frames, silent_run
        # 読み上げ中の短い物音(割り込みに満たないもの)は捨てる
        keep = voiced_frames * FRAME_S >= MIN_VOICED_S and not (speaking and not barged)
        if keep:
            jobs.put(b"".join(segment))
            if from_file:
                jobs.join()  # 検証では、区切りごとに結果を待って判定の位置をそろえる
        segment, in_speech, voiced_frames, silent_run = [], False, 0, 0

    for frame in frames:
        elapsed += FRAME_S
        voiced = vad.is_speech(frame, RATE)
        recent.append(voiced)
        if args.verbose:
            total_frames += 1
            total_voiced += voiced
            peak = max(peak, max(abs(v) for v in array.array("h", frame)))
        if voiced:
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
            elif not barged and in_speech and voiced_frames * FRAME_S >= BARGE_VOICED_S:
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
        if since_voice >= args.idle and not in_speech:
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
    notes = {
        "idle": f"[打ち切り: {args.idle:g} 秒間、声が聞こえませんでした。「{args.end_word}」は聞こえていません]",
        "max": f"[打ち切り: 制限時間({args.max:g} 秒)に達しました。「{args.end_word}」は聞こえていません]",
        "mute": "[ミュート: 利用者が「ミュート」と言いました。マイクを閉じました]",
        "interrupted": "[中断されました]",
        "eof": f"[音声ファイルの終わりに達しました。「{args.end_word}」は聞こえていません]",
    }
    if reason in notes:
        print(notes[reason])
    return 0


if __name__ == "__main__":
    sys.exit(main())
