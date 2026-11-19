#!/bin/bash
# 音声会話用のローカルサービス(Whisper、Kokoro)を切り替える。
# 使い方: voice.sh on | off | status | listen [listen.py の引数] | say '文' | yomi '表記' '読み' | mic builtin|default | review [日付] | release | end | cancel | notify | stop-check | captions on|off
# 何度実行しても同じ結果になる。
set -u

VM="$HOME/.local/bin/voicemode"
ENV_FILE="$HOME/.voicemode/voicemode.env"
PY="$HOME/.local/share/uv/tools/voice-mode/bin/python"
WHISPER_SH="$HOME/.voicemode/services/whisper/bin/start-whisper-server.sh"
KOKORO_SH="$HOME/.voicemode/services/kokoro/start-gpu_mac.sh"
WHISPER_URL="http://127.0.0.1:2022/health"
KOKORO_URL="http://127.0.0.1:8880/v1/audio/voices"
CAPTION_SRC="$(dirname "$0")/caption.swift"
CAPTION_BIN="$HOME/.voicemode/10-voice-caption"
SESSIONS_FILE="$HOME/.voicemode/10-voice-sessions.json"   # listen.py の SESSIONS_PATH と同じ
CAPTION_DIR="$HOME/.voicemode/10-voice-captions"   # listen.py の CAPTION_DIR と同じ。日付ごとのファイルが入る

die() { echo "エラー: $*" >&2; exit 1; }

up() { curl -s -o /dev/null -m 2 "$1"; }

listen_addr() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}' | sort -u | tr '\n' ' '; }

# 外部に音声を送らない設定になっているかを確かめる。崩れていたら起動しない。
check_privacy() {
  [ -x "$VM" ] || die "voicemode が見つかりません: $VM"
  [ -f "$ENV_FILE" ] || die "設定ファイルがありません: $ENV_FILE"
  local key urls
  for key in VOICEMODE_TTS_BASE_URLS VOICEMODE_STT_BASE_URLS; do
    urls=$(grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2-)
    [ -n "$urls" ] || die "$key が未設定です。既定値には OpenAI が含まれるため、ローカルの URL を設定してください"
    if echo "$urls" | tr ',' '\n' | grep -qvE '^https?://(127\.0\.0\.1|localhost)[:/]'; then
      die "$key にローカル以外の接続先があります: $urls"
    fi
  done
  if [ -n "${OPENAI_API_KEY:-}" ] || grep -qE '^OPENAI_API_KEY=.+' "$ENV_FILE"; then
    die "OPENAI_API_KEY が設定されています。外部送信を防ぐため、外してください"
  fi
}

# 起動スクリプトの待ち受けを Mac 内だけにする。VoiceMode の更新で戻ることがあるため毎回確かめる。
fix_bind() {
  local f
  for f in "$WHISPER_SH" "$KOKORO_SH"; do
    [ -f "$f" ] || die "起動スクリプトがありません: $f"
    if grep -q -- '--host 0\.0\.0\.0' "$f"; then
      sed -i '' 's/--host 0\.0\.0\.0/--host 127.0.0.1/g' "$f"
      echo "待ち受けを 127.0.0.1 に直しました: $f"
      BIND_FIXED=1
    fi
  done
}

wait_up() {
  local i
  for i in $(seq 1 60); do
    up "$1" && return 0
    sleep 2
  done
  return 1
}

start_one() { # 名前 確認用URL ポート
  if up "$2" && [ "${BIND_FIXED:-0}" = 0 ]; then
    echo "$1: すでに起動しています"
  else
    [ "${BIND_FIXED:-0}" = 1 ] && "$VM" service stop "$1" >/dev/null 2>&1
    "$VM" service start "$1" >/dev/null 2>&1
    wait_up "$2" || die "$1 が起動しませんでした。'$VM service logs $1' で確認してください"
    echo "$1: 起動しました"
  fi
  case "$(listen_addr "$3")" in
    "127.0.0.1:$3 ") ;;
    *) die "$1 の待ち受けが 127.0.0.1 だけになっていません: $(listen_addr "$3")" ;;
  esac
}

stop_one() { # 名前 ポート
  # disable は停止と自動起動の解除を兼ねる。登録がない場合に備えて stop も呼ぶ。
  "$VM" service disable "$1" >/dev/null 2>&1
  "$VM" service stop "$1" >/dev/null 2>&1
  local i
  for i in $(seq 1 10); do
    [ -z "$(listen_addr "$2")" ] && { echo "$1: 停止しました"; return 0; }
    sleep 1
  done
  die "$1 が停止しませんでした(ポート $2 が使用中)"
}

status_one() { # 名前 確認用URL ポート
  if up "$2"; then echo "$1: 起動中($(listen_addr "$3"))"; else echo "$1: 停止中"; fi
}

caption_running() { pgrep -f "$CAPTION_BIN $CAPTION_DIR" >/dev/null 2>&1; }

# 字幕の窓を出す。失敗しても会話はできるので、エラーでは止めない。
caption_on() {
  if [ ! -x "$CAPTION_BIN" ] || [ "$CAPTION_SRC" -nt "$CAPTION_BIN" ]; then
    command -v swiftc >/dev/null 2>&1 || { echo "字幕: swiftc がないため表示しません(Xcode のコマンドラインツールが必要)"; return 0; }
    # 別名でビルドし、成功したときだけ入れ替える。失敗しても、動いている窓は残す
    if ! swiftc -O -o "$CAPTION_BIN.new" "$CAPTION_SRC" 2>"$CAPTION_BIN.err"; then
      echo "字幕: ビルドに失敗しました。以前の版があれば、それを使います"
      grep -m 3 'error:' "$CAPTION_BIN.err"
      rm -f "$CAPTION_BIN.new"
      [ -x "$CAPTION_BIN" ] || return 0
    else
      pkill -f "$CAPTION_BIN " >/dev/null 2>&1
      mv "$CAPTION_BIN.new" "$CAPTION_BIN"
    fi
    rm -f "$CAPTION_BIN.err"
  fi
  mkdir -p "$CAPTION_DIR" && chmod 700 "$CAPTION_DIR"
  if caption_running; then
    echo "字幕: すでに表示しています"
  else
    nohup "$CAPTION_BIN" "$CAPTION_DIR" "$SESSIONS_FILE" >/dev/null 2>&1 &
    echo "字幕: 表示しました"
  fi
}

# 字幕の窓を閉じる。字幕のファイルは残す。
caption_off() {
  pkill -f "$CAPTION_BIN " >/dev/null 2>&1
  echo "字幕: 閉じました"
}

memo_running() { pgrep -f "listen.py --memo-watch" >/dev/null 2>&1; }

# メモの見張りを始める。Claude の作業中に、キーを押して話したメモを預かる。失敗しても会話はできる。
memo_on() {
  [ -x "$PY" ] || return 0
  if memo_running; then
    # スクリプトが新しくなっていたら、動かし直す
    [ "$(dirname "$0")/listen.py" -nt "$HOME/.voicemode/10-voice-memo-watch.lock" ] || { echo "メモ: 見張りは動いています"; return 0; }
    pkill -f "listen.py --memo-watch" >/dev/null 2>&1
    for _ in 1 2 3 4 5 6 7 8 9 10; do memo_running || break; sleep 0.5; done   # 古い見張りが終わるのを待つ
  fi
  touch "$HOME/.voicemode/10-voice-memo-watch.lock"
  nohup "$PY" "$(cd "$(dirname "$0")" && pwd -P)/listen.py" --memo-watch >/dev/null 2>"$HOME/.voicemode/10-voice-memo-watch.err" &
  sleep 1
  if memo_running; then echo "メモ: 見張りを始めました"; else echo "メモ: 見張りを始められませんでした(作業中のメモは使えません。会話はできます)"; fi
}

memo_off() {
  pkill -f "listen.py --memo-watch" >/dev/null 2>&1
  echo "メモ: 見張りを止めました"
}

case "${1:-}" in
  on)
    check_privacy
    fix_bind
    start_one whisper "$WHISPER_URL" 2022
    start_one kokoro "$KOKORO_URL" 8880
    caption_on
    memo_on
    "$PY" "$(dirname "$0")/listen.py" --unmute 2>/dev/null   # このセッションがミュート中なら、解く
    "$PY" "$(dirname "$0")/listen.py" --print-mode 2>/dev/null
    ;;
  off)
    memo_off
    caption_off
    stop_one whisper 2022
    stop_one kokoro 8880
    ;;
  status)
    status_one whisper "$WHISPER_URL" 2022
    status_one kokoro "$KOKORO_URL" 8880
    if [ -x "$PY" ]; then "$PY" "$(dirname "$0")/listen.py" --print-mode 2>/dev/null; fi
    ;;
  listen)
    # 読み上げてから、利用者の発言を聞き取り、文字にして出力する(キーを押している間だけ聞く。--mode word で合言葉の方式)。
    shift
    [ -x "$PY" ] || die "VoiceMode の Python が見つかりません: $PY"
    up "$WHISPER_URL" || die "Whisper が起動していません。先に '$0 on' を実行してください"
    exec "$PY" "$(dirname "$0")/listen.py" "$@"
    ;;
  say)
    # 返事のいらない一言を読み上げる。聞き取らずに終わり、順番を手放す(作業に入る前の一言など)。
    shift
    [ -x "$PY" ] || die "VoiceMode の Python が見つかりません: $PY"
    [ -n "${1:-}" ] || { echo "使い方: $0 say '読み上げる文'" >&2; exit 2; }
    up "$KOKORO_URL" || die "Kokoro が起動していません。先に '$0 on' を実行してください"
    # 一言のために長く待たない。ほかのセッションが話していて順番が来なければ、読み上げずに戻る
    exec "$PY" "$(dirname "$0")/listen.py" --no-reply --wait 20 --say "$1"
    ;;
  review)
    # 話し方の分析のために、1 日分の会話の文字から、利用者の発言と数の集計を取り出す(日付を省くと今日)。
    shift
    exec python3 "$(dirname "$0")/review.py" "$@"
    ;;
  mic)
    # 聞き取りに使うマイクを選ぶ(全セッションで共通)。builtin: Mac 本体のマイク。default: 既定の入力(イヤホンなど)。
    case "${2:-}" in
      builtin|default) exec "$PY" "$(dirname "$0")/listen.py" --mic "$2" ;;
      *) echo "使い方: $0 mic builtin | default" >&2; exit 2 ;;
    esac
    ;;
  yomi)
    # 読みがなを登録する。字幕には表記のまま出し、声では読みで読む。利用者の表(Mac の中だけ)に書く。
    [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "使い方: $0 yomi '表記' '読み' ['聞き間違い' ...]" >&2; exit 2; }
    [ -x "$PY" ] || die "VoiceMode の Python が見つかりません: $PY"
    shift
    exec "$PY" "$(dirname "$0")/listen.py" --yomi "$@"   # 表記、読み、(あれば)聞き間違い
    ;;
  cancel)
    # このセッションの、動いている聞き取り(バックグラウンドで待っているものを含む)を止める。
    [ -x "$PY" ] || exit 0
    exec "$PY" "$(dirname "$0")/listen.py" --cancel
    ;;
  stop-check)
    # Claude Code の hook(Stop)から呼ぶ。音声会話の途中で、聞き取りを呼ばずにターンを終えようとしたら、引き止める。
    [ -x "$PY" ] || exit 0
    exec "$PY" "$(dirname "$0")/listen.py" --stop-check
    ;;
  notify)
    # Claude Code の hook から呼ぶ。実行の確認が画面に出たことを、音声で会話中のセッションに限って声で知らせる。
    # 標準入力の JSON(作業フォルダを含む)を、そのまま渡す。サービスが止まっていれば、何もしない。
    [ -x "$PY" ] || exit 0
    up "$KOKORO_URL" || exit 0
    exec "$PY" "$(dirname "$0")/listen.py" --notify
    ;;
  release)
    # 時間切れのあとに持ち続けている順番を手放す。ほかのセッションが、待たずに話せるようになる。
    [ -x "$PY" ] || die "VoiceMode の Python が見つかりません: $PY"
    exec "$PY" "$(dirname "$0")/listen.py" --release
    ;;
  end)
    # 音声での会話を終える。順番を手放し、字幕の窓のタブから、このセッションを消す。
    [ -x "$PY" ] || die "VoiceMode の Python が見つかりません: $PY"
    exec "$PY" "$(dirname "$0")/listen.py" --end
    ;;
  captions)
    case "${2:-}" in
      on) caption_on ;;
      off) caption_off ;;
      *) echo "使い方: $0 captions on | off" >&2; exit 2 ;;
    esac
    ;;
  *)
    echo "使い方: $0 on | off | status | listen [引数] | say '文' | yomi '表記' '読み' | mic builtin|default | review [日付] | release | end | cancel | notify | stop-check | captions on|off" >&2
    exit 2
    ;;
esac
