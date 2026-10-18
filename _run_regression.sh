# _run_regression.sh —— 串行跑全量回归（唯一的正确入口）
: <<'DOC'

【为什么要固化成脚本 · 2026-10-18 第三次踩同一个坑】
手写 for 循环挑脚本名，已经三次写错成不存在的名字：

    verify_keyhold.py    → 真名 regression_keyhold.py
    verify_keypause.py   → 真名 diag_keypause_probe.py
    verify_settings.py   → 真名 diag_settings_probe.py
    verify_globalpause.py→ 真名 diag_globalpause_probe.py

Python 找不到文件只返回 EXIT=2，日志里一行
`can't open file ...` 混在几千行输出中，grep "失败" 根本抓不到。
所以名单只在这一处维护，跑之前逐个核对，缺一个就中止；
任意套件非 0 时，本脚本自身也必须非 0，不能只打印红字却伪装成功。

用法：bash _run_regression.sh（约 6~10 分钟，必须串行）
DOC

bash -c 'true' 2>/dev/null || { echo "需要 bash"; exit 1; }

cd "$(dirname "$0")"

PY=".venv/Scripts/python.exe"
VPY="C:/Users/LXZ/.workbuddy/binaries/python/envs/vsum/Scripts/python.exe"
LOG="_reg_latest.log"

SUITES="
verify_window.py
verify_mouse_zone.py
verify_binding.py
verify_trigger.py
verify_bugfix.py
verify_two_stage.py
verify_config_io.py
verify_toast_delivery.py
verify_toast_style.py
verify_toast.py
verify_reported_20261016.py
regression_keyhold.py
diag_keypause_probe.py
diag_settings_probe.py
diag_globalpause_probe.py
diag_v22_features_probe.py
diag_v23_features_probe.py
diag_v24_tray_probe.py
diag_v25_vscreen_probe.py
diag_v26_multipos_probe.py
diag_v27_macro_probe.py
diag_v30_editor_probe.py
diag_v34_probe.py
diag_v35_e2e_replay.py
diag_bg_probe.py
diag_v41_flow_probe.py
"

echo "[0/2] 逐个核对脚本存在（名字写错会静默跳过，必须先卡住）"
MISS=0
for f in $SUITES; do
  if [ ! -f "$f" ]; then echo "  [缺] $f"; MISS=1; fi
done
if [ "$MISS" = "1" ]; then
  echo "清单里有不存在的文件，先修正 _run_regression.sh 的 SUITES"
  exit 1
fi
echo "  共 $(echo $SUITES | wc -w) 个套件，全部存在"

echo "[1/2] 串行执行（并行会抢 CPU 造成计时断言假红）"
: > "$LOG"
FAIL=0
for f in $SUITES; do
  echo "########## $f" >> "$LOG"
  "$PY" "$f" >> "$LOG" 2>&1
  RC=$?
  echo "EXIT=$RC" >> "$LOG"
  if [ "$RC" != "0" ]; then FAIL=1; fi
done
echo "########## selftest.py" >> "$LOG"
"$VPY" selftest.py >> "$LOG" 2>&1
RC=$?
echo "EXIT=$RC" >> "$LOG"
if [ "$RC" != "0" ]; then FAIL=1; fi

echo "[2/2] 结果"
echo "--- 非 0 退出（！！！ 2 = 文件不存在，不是真失败）---"
grep -n "^EXIT=[^0]" "$LOG" || echo "  无"
echo "--- 有 Traceback 但没 EXIT 标记的套件 ---"
grep -n "Traceback" "$LOG" | head
echo "--- 各套汇总行 ---"
grep -n "通过 / \|汇总：\|结论：" "$LOG" | tail -30
echo "日志：$LOG"
exit "$FAIL"
