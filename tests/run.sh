#!/bin/bash
# 声を使わずに確かめられるテストを、まとめて実行する。
# 使い方: tests/run.sh
# マイクもスピーカーも使わず、Whisper と Kokoro にも接続しない。利用者の ~/.voicemode には触れない。
# 実際の声と操作が要る確認(割り込み、順番待ち、字幕の窓)は、含まない。
set -u
cd "$(dirname "$0")/.." || exit 1
S=skills/10-voice/scripts
fail=0
step() { # 名前 コマンド...
  local name="$1"; shift
  if out=$("$@" 2>&1); then echo "ok   $name"; else echo "NG   $name"; echo "$out" | sed 's/^/     /'; fail=1; fi
}

# listen.py は httpx と webrtcvad を使う。VoiceMode と一緒に入った Python があれば、それを使う
PY="$HOME/.local/share/uv/tools/voice-mode/bin/python"
[ -x "$PY" ] || PY=python3

echo "== 文法 =="
step "voice.sh" bash -n "$S/voice.sh"
step "tests/test_voice_sh.sh" bash -n tests/test_voice_sh.sh
# 一時フォルダに書き出して、リポジトリに __pycache__ を作らない
step "listen.py" "$PY" -c 'import sys, py_compile, tempfile, os; py_compile.compile(sys.argv[1], cfile=os.path.join(tempfile.mkdtemp(), "x.pyc"), doraise=True)' "$S/listen.py"
step "review.py" python3 -c 'import sys, py_compile, tempfile, os; py_compile.compile(sys.argv[1], cfile=os.path.join(tempfile.mkdtemp(), "x.pyc"), doraise=True)' "$S/review.py"
if command -v swiftc >/dev/null 2>&1; then
  step "caption.swift" swiftc -typecheck "$S/caption.swift"
else
  echo "--   caption.swift(swiftc がないので、飛ばしました)"
fi

echo "== 読みがなの表 =="
# 「表記<TAB>読み」の 2 列で、同じ表記が 2 回出てこないこと
step "yomi.tsv の書式" awk -F'\t' '/^#/ || /^$/ { next } NF != 2 || $1 == "" || $2 == "" { print "書式: " NR ": " $0; bad = 1 } seen[tolower($1)]++ { print "重複: " NR ": " $1; bad = 1 } END { exit bad }' "$S/yomi.tsv"

echo "== listen.py =="
if "$PY" -c 'import httpx, webrtcvad' 2>/dev/null; then
  PYTHONDONTWRITEBYTECODE=1 "$PY" tests/test_listen.py || fail=1
else
  echo "NG   httpx と webrtcvad のある Python が見つかりません(VoiceMode を入れると、一緒に入ります)"; fail=1
fi

echo "== voice.sh =="
bash tests/test_voice_sh.sh || fail=1

echo
if [ "$fail" = 0 ]; then echo "すべて通りました"; else echo "通らなかったテストがあります"; fi
exit "$fail"
