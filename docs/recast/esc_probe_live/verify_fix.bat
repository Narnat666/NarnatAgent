@echo off
rem FIX-ESC 修复后验证矩阵（真机，E2 装置）
rem 验收目标：
rem   1) esc_then_backtick：修复前=完全吞键（无中断）；修复后=即时中断（<0.6s）
rem   2) dense_alternate：修复前=靠第 2 个 Esc（0.35~0.4s）；修复后=首个 Esc 即中断
rem   3) baseline/alternate/rage：修复前慢路径可达 7~11.5s（概率 ~1/3）；修复后应全部 <1.5s
cd /d D:\desktop\NarnatAgent\docs\recast\esc_probe_live

for %%i in (1 2 3) do python runner.py --version new --plan esc_then_backtick --attempt %%i --console wt
for %%i in (1 2 3) do python runner.py --version new --plan dense_alternate --attempt %%i --console wt
for %%i in (1 2 3 4 5 6) do python runner.py --version new --plan baseline --attempt %%i --console wt
for %%i in (1 2 3) do python runner.py --version new --plan alternate --attempt %%i --console wt
for %%i in (1 2 3) do python runner.py --version new --plan rage --attempt %%i --console wt

python summarize.py
echo VERIFY_MATRIX_DONE
