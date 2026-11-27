#!/bin/bash
# voice.sh の、起動の前の確認(音声を Mac の外へ出さない設定になっているか)のテスト。
# ホームを一時フォルダに差し替えて実行する。確認を通った場合も、起動スクリプトがないので、サービスは起動しない。
# 利用者の設定と、動いているサービスには触れない(止める命令や、字幕の窓の命令は呼ばない)。
set -u
VOICE="$(cd "$(dirname "$0")/../skills/10-voice/scripts" && pwd -P)/voice.sh"
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/.local/bin" "$T/.voicemode"
printf '#!/bin/sh\nexit 0\n' > "$T/.local/bin/voicemode"
chmod +x "$T/.local/bin/voicemode"
ENV="$T/.voicemode/voicemode.env"
GOOD_TTS='VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880/v1'
GOOD_STT='VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1'
PASSED='起動スクリプトがありません'   # 確認を通ったあとに出る文(一時のホームには、起動スクリプトがない)

fail=0
n=0

run_on() { env -i HOME="$T" PATH="$PATH" "$@" bash "$VOICE" on 2>&1; }

# 名前、期待(stop: 確認で止まる / pass: 確認を通る)、出力に含まれるはずの文、設定の行...
check() {
  local name="$1" want="$2" phrase="$3"; shift 3
  printf '%s\n' "$@" > "$ENV"
  local out; out=$(run_on)
  local got=stop; case "$out" in *"$PASSED"*) got=pass ;; esac
  n=$((n + 1))
  if [ "$got" = "$want" ] && { [ -z "$phrase" ] || [[ "$out" == *"$phrase"* ]]; }; then
    echo "ok   $name"
  else
    echo "NG   ${name}(期待: ${want}、結果: ${got})"; echo "     出力: $out"; fail=$((fail + 1))
  fi
}

check "Mac 内だけの設定は通る" pass "" "$GOOD_TTS" "$GOOD_STT"
check "localhost も通る" pass "" 'VOICEMODE_TTS_BASE_URLS=http://localhost:8880/v1' "$GOOD_STT"
check "読み上げの接続先が未設定なら止まる" stop "未設定" "$GOOD_STT"
check "聞き取りの接続先が未設定なら止まる" stop "未設定" "$GOOD_TTS"
check "OpenAI の接続先は止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS=https://api.openai.com/v1' "$GOOD_STT"
check "ローカルと外部が並んでいても止まる" stop "ローカル以外" "$GOOD_TTS" 'VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1,https://api.openai.com/v1'
check "あとの行が外部なら止まる(あとの行が効く)" stop "ローカル以外" "$GOOD_TTS" "$GOOD_STT" 'VOICEMODE_STT_BASE_URLS=https://api.openai.com/v1'
check "LAN のアドレスは止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS=http://192.168.1.10:8880/v1' "$GOOD_STT"
check "localhost で始まる別のホストは止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS=http://localhost.example.com/v1' "$GOOD_STT"
check "127.0.0.1 で始まる別のホストは止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS=http://127.0.0.1.example.com/v1' "$GOOD_STT"
check "@ で別のホストに向ける書き方は止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880@example.com/v1' "$GOOD_STT"
check "@ で別のホストに向ける書き方は止まる(localhost)" stop "ローカル以外" "$GOOD_TTS" 'VOICEMODE_STT_BASE_URLS=http://localhost:x@example.com/v1'
check "設定ファイルの API キーは止まる" stop "OPENAI_API_KEY" "$GOOD_TTS" "$GOOD_STT" 'OPENAI_API_KEY=sk-test'
check "export 付きの API キーは止まる" stop "OPENAI_API_KEY" "$GOOD_TTS" "$GOOD_STT" 'export OPENAI_API_KEY=sk-test'
check "export 付きの外部の接続先は止まる" stop "" "$GOOD_TTS" "$GOOD_STT" 'export VOICEMODE_STT_BASE_URLS=https://api.openai.com/v1'
check "空の API キーの行は通る" pass "" "$GOOD_TTS" "$GOOD_STT" 'OPENAI_API_KEY='

check "引用符付きのローカルの接続先は通る" pass "" 'VOICEMODE_TTS_BASE_URLS="http://127.0.0.1:8880/v1"' "$GOOD_STT"
check "引用符付きの外部の接続先は止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS="https://api.openai.com/v1"' "$GOOD_STT"
check "ポートが数字でない接続先は止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:80.example.com/v1' "$GOOD_STT"
check "http でない接続先は止まる" stop "ローカル以外" 'VOICEMODE_TTS_BASE_URLS=ftp://127.0.0.1/v1' "$GOOD_STT"

# 環境変数の接続先(設定ファイルより優先される)
printf '%s\n' "$GOOD_TTS" "$GOOD_STT" > "$ENV"
out=$(run_on VOICEMODE_STT_BASE_URLS=https://api.openai.com/v1); n=$((n + 1))
if [[ "$out" == *"ローカル以外"* && "$out" != *"$PASSED"* ]]; then echo "ok   環境変数の外部の接続先は止まる"; else echo "NG   環境変数の外部の接続先は止まる"; echo "     出力: $out"; fail=$((fail + 1)); fi

# いまのフォルダや、その上にある設定ファイル(ホームのものより優先される)
mkdir -p "$T/proj/sub"
printf '%s\n' 'VOICEMODE_TTS_BASE_URLS=https://api.openai.com/v1' > "$T/proj/.voicemode.env"
out=$(cd "$T/proj/sub" && run_on); n=$((n + 1))
if [[ "$out" == *"ローカル以外"* && "$out" != *"$PASSED"* ]]; then echo "ok   上のフォルダの設定ファイルの外部の接続先は止まる"; else echo "NG   上のフォルダの設定ファイルの外部の接続先は止まる"; echo "     出力: $out"; fail=$((fail + 1)); fi
printf '%s\n' 'OPENAI_API_KEY=sk-test' > "$T/proj/.voicemode.env"
out=$(cd "$T/proj/sub" && run_on); n=$((n + 1))
if [[ "$out" == *"OPENAI_API_KEY"* && "$out" != *"$PASSED"* ]]; then echo "ok   上のフォルダの設定ファイルの API キーは止まる"; else echo "NG   上のフォルダの設定ファイルの API キーは止まる"; echo "     出力: $out"; fail=$((fail + 1)); fi
rm -rf "$T/proj"

# 環境変数の API キー
printf '%s\n' "$GOOD_TTS" "$GOOD_STT" > "$ENV"
out=$(run_on OPENAI_API_KEY=sk-test); n=$((n + 1))
if [[ "$out" == *"OPENAI_API_KEY"* && "$out" != *"$PASSED"* ]]; then echo "ok   環境変数の API キーは止まる"; else echo "NG   環境変数の API キーは止まる"; echo "     出力: $out"; fail=$((fail + 1)); fi

# 設定ファイルがない
rm -f "$ENV"; out=$(run_on); n=$((n + 1))
if [[ "$out" == *"設定ファイルがありません"* ]]; then echo "ok   設定ファイルがなければ止まる"; else echo "NG   設定ファイルがなければ止まる"; echo "     出力: $out"; fail=$((fail + 1)); fi

# 引数の誤り(どれも、何も起動せずに、使い方を出して終わる)
for args in "" "unknown" "yomi" "yomi 表記" "mic" "mic other" "captions" "captions other"; do
  # shellcheck disable=SC2086
  env -i HOME="$T" PATH="$PATH" bash "$VOICE" $args >/dev/null 2>"$T/err"; code=$?
  n=$((n + 1))
  if [ "$code" = 2 ] && grep -q "使い方" "$T/err"; then echo "ok   引数「${args}」は使い方を出して終わる"; else echo "NG   引数「${args}」(終了コード ${code})"; fail=$((fail + 1)); fi
done

echo "voice.sh: $n 件中 $((n - fail)) 件が通りました"
[ "$fail" = 0 ]
