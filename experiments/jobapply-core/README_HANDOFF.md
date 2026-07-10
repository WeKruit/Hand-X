# HANDOFF — observe_act 泛化填表 · P1-P3 swarm 战役（2026-07-10）

> 给下一个 session 的完整交接。主线 = **把 job application 真正填上并用像素证明**。
> 引擎自报从来不是真相：任何 FILLED/DONE 都要过对抗式截图审计才算数。

---

## 0. 一句话现状

- 引擎分支：`feat/observe-act-generic`，tip **`178a3a61b`**（代码 tip `399f47bd1` + handoff chore）
- 工作树：`Hand-X/.claude/worktrees/observe-act-generic/experiments/jobapply-core`
- 修复前基线：sweep500b 自报 84.1%（460 run），审计后真实 **72.6%**（captcha 踢分母 75.7%）
- P1+P2 修复后：失败队列 166 行重跑 FILLED 49→**103**；P3 重放 58 行逐字段 **922/1003 = 91.9% DONE**
- P3 审计（56 行）：**25 真绿 / 15 假绿 / 3 真 NEEDS_HUMAN / 13 NH-残余** → 拆出 F1/F2/F3 三个新家族（见 §4）
- 正式数字尚未产出 —— 等 F1-F3 落地 + P5 + FINAL 全量 sweep（目标审计后 ≥90%，冲 95）

## 1. 铁律（用户定的 PRINCIPLES，存了 memory，违反 = 打回）

1. **fixture-first**：每个线上失败必须先用 `fixture_miner.py` 挖**真实 DOM** 复刻成 playground fixture（RED），修完转 GREEN 才准合并。手写想象的 fixture 是被实锤坑过的反模式。
2. **视觉观察是无条件节拍**：每 K=5 字段一次视觉检查点 + 页面处理完 flush 残余 + 任何翻页/提交前强制 flush。vision 链 gemini→OpenAI→重试→全败=UNVERIFIED（永不 COMPLETE），禁止 fail-open。+$0.01/app 已批。
3. **禁止静态文本模式匹配**：`==`/includes/startsWith/regex/字数阈值 全禁。定位靠结构（roles/geometry/containment），匹配靠意义（LLM/vision）。
4. **秘密只走环境变量**（ps aux 可见 CLI argv）；**永不 `fly secrets set`**（ATM/Infisical 是唯一源）。
5. **验收要独立**：agent 自报不算；复跑它的测试、亲读截图、`git merge-base` 查基座。

## 2. 命令手册（全部实测过的签名）

```bash
cd "Hand-X/.claude/worktrees/observe-act-generic/experiments/jobapply-core"
# 依赖: python3.12 + playwright chromium; API keys 在环境变量（GH_ANTHROPIC_API_KEY 等）
```

### 单 URL live 跑（调试/验证必用）
```bash
OA_VLM_TIMEOUT=12 python3 oa_singlepage.py \
  --url "https://jobs.ashbyhq.com/<tenant>/<id>/application" --generic \
  --profile fixtures/rich_profile.json \
  --resume fixtures/resumes/test_resume.pdf \
  --json /tmp/out.json --screenshot /tmp/out.png
# profiles: rich_profile / rich_profile2 / profile_intl / minimal / veteran (fixtures/)
# 跑完必须亲眼 Read 截图 —— json 里的 status 不是真相
```

### 批量 sweep + 只重跑失败
```bash
cd runs/newats
python3 sweep500_run.py <并发数> <tsv文件>        # 例: python3 sweep500_run.py 5 sweep500b.tsv
                                                  # 输出 <tsv名>_results.json + 同名目录/NNN.json+png
OA_RUN_TIMEOUT=170 ...                            # 每 run 超时（env）
python3 rerun_failed.py [conc]                    # 从 ledger 非COMPLETE + audit_false_greens.json
                                                  # 生成 rerun_failed.tsv 并调 sweep500_run
python3 rerun_failed.py --tsv-only                # 只生成 tsv 不跑
python3 sweep500_score.py <results.json>          # 打分（默认 sweep500b_results.json）
```
**坑**：干净重跑要同时删 `<tag>_results.json` **和**输出目录 —— 只删目录会读到旧累加器报 "0 to run"。

### Playground 门禁（137 fixtures）
```bash
cd runs/fixtures
python3 selfcheck.py                              # 全量门禁；passing set 不许缩
python3 fixture_miner.py --help                   # 从失败 run 的 URL 挖真 DOM 生成 fixture
                                                  # (data-read oracle + data-actuate)
```

### 对抗式截图审计（唯一的真相层）
1. 造 manifest（每行 `{idx, png, company, status, focus:[字段label前缀]}`），参考 `runs/newats/audit_p3.json`
2. 每行派一个 vision agent 读 png：FILLED 行 → 逐 focus 字段"像素上真的答了吗"（pill 双灰=没答、
   placeholder=没填、check 未勾=没答），拿不准判 FALSE_GREEN；NEEDS_HUMAN 行 → 真有 captcha/遮挡
   弹层/录音题吗（角落 reCAPTCHA 徽标不算）→ REAL_NH / OVER_CONSERVATIVE
3. verdict 枚举：`REAL_GREEN | FALSE_GREEN | REAL_NH | OVER_CONSERVATIVE | NO_SCREENSHOT`
4. 已用的 workflow 脚本可抄：本 session `workflows/scripts/p3-screenshot-audit-*.js`（56 并行 agent，~5 分钟）

### 关键环境变量
```bash
OA_VLM_TIMEOUT=12        # 5s 太紧会把 vision 掐死 → 假绿守卫失效
OA_VISUAL_EVERY=5        # 视觉检查点节拍 K
OA_OPENAI_VLM_MODEL=...  # vision 供应商链第二跳（已存在于环境）
GH_VERIFY_MAX_CALLS=64   # vision 调用护栏（原 6 会饿死验证）
OA_RUN_TIMEOUT=170       # sweep 每 run 超时
```
**live sweep 必须不带沙箱跑**：沙箱限流 Gemini → VLM 超时 → 视觉假绿守卫静默死亡（实锤过）。

## 3. 任务面板（harness TaskList，另 session 重建时照抄）

| # | 任务 | 状态 |
|---|---|---|
| 1-4,6 | P0 scorer / P0.5 miner / P1 视觉检查点+供应商链 / P2 视觉提交 / P4 rerun | ✅ 已关（全部截图验收）|
| 5 | P3 EEO+iframe+banner | 🔶 EEO 本体像素验证通过；关闭条件 = F1-F3 落地 → 28 行重跑 → 再审计 |
| 9 | P3-B 跨域 iframe（coreweave）| 🔶 代码质量 OK 但**基座过期**被打回：rebase 到引擎 tip + 4 gate 重跑中 |
| 10 | P3-D consent 去英文词表 | 🔶 进行中，已预警同样的基座问题（rebase 后再跑 gate）|
| 11 | **P3-F1** domref-choice-commit 绕过 S_VERIFY | 🔶 f1-engineer 修复中（隔离 worktree /tmp/wt_f1）|
| 12 | **P3-F2** 串值污染 blank->SKIP 字段 | 🔶 f2-engineer 修复中（隔离 worktree /tmp/wt_f2）|
| 13 | **P3-F3** run verdict 无视字段级 ESCALATE | ⬜ 待派（spec 在任务描述里，含 anthropic 048 定性）|
| 7 | P5 playground 闭环 | ⬜ 被 5 阻塞：补 7 类 fixture + 全量门禁 + 闭环测试 |
| 8 | FINAL 全量 fresh sweep + 全量审计 | ⬜ 被 5,11,12,13 阻塞：出正式数（≥90 冲 95）|

## 4. P3 审计拆出的三个家族（新 session 的最高优先级）

**F1（8 run）**：`_domref_choice_commit`（oa_observe_act.py）trace 三步
`S0_GUARD→S1_LOCATE→domref-choice-commit:Yes` 直出 DONE，**不进 S_VERIFY**。
原生 radio CSS `:checked` 上色侥幸真绿；Ashby React pill next-tick class 不重绘 → 像素双灰。
受害:vanta 007/031/039/047/052（legally-authorized pill）、replit 013/045/051（Foster City pill）。
修法：lane 必须终结于 S_VERIFY+视觉检查点；触发收窄到真不可见控件（结构判定）。

**F2（6 run）**：被污染字段 ledger 记 `blank->SKIP committed=''`，像素却有 '+1'/'+44'/essay
—— **别的字段动作串进来的**（焦点竞态，疑电话国码 widget / textarea 打字），且 SKIP 字段永不复检。
受害:replit 006/013/017/037/045（Profile URL）、airwallex 042（Middle Name）。
修法：打字前结构校验焦点身份（backendNodeId 比对）+ blank-SKIP 字段进 checkpoint watchlist（非空=污染→清）。

**F3（2 run + 048）**：sierra 038/046 字段级 trace 明写失败（`still-blank→ESCALATE`、
`recommit-verdict:EMPTY→commit-cap`）但 run 报 FILLED —— 聚合层不看字段级失败；required 开放题
（deal size/junk food）blank->SKIP 不许留白（LLM 生成或 NEEDS_HUMAN）。
anthropic 048 zoom 复审实锤假绿（DOM-verified 'Marcus' 但 420px 输入框零暗像素）= GH render-desync/wipe，归 #15 族。

**NH-残余 13 run**：不是误报 —— 末端 gate 发现 geocomplete/pill 没填好整单降级。F1/F2 修完大半翻绿。

## 5. 数据/产物地图

```
runs/newats/sweep500b.tsv + sweep500b/ + sweep500b_results.json   # 500 行主战场（460 有效）
runs/newats/sweep500b_audit_final.json                            # 381 行全量审计总账
runs/newats/audit_false_greens.json                               # rerun_failed 的输入之一
runs/newats/p3_targets/ + audit_p3.json                           # P3 重放 58 行 + 审计清单
runs/newats/rerun_failed.{py,tsv} rerun_failed/                   # 失败重跑基建
runs/fixtures/all_fixtures.json (137) + selfcheck.py + fixture_miner.py
```
P5 还差的 7 类 fixture：arketa/replit pill 变体、onepassword 双下拉、白色卡死弹层、
cookie banner（P3-D 产出）、级联追问、串值 bleed（F2 产出）、双标签假红/wipe（#15，含 anthropic 048）。

## 6. 坑清单（每条都流过血）

1. **`bool(page.evaluate(...))` 字符串陷阱**：browser_use evaluate 返回序列化字符串，`bool('False')=True`。
   写法 = 裸箭头 + `'yes'/'no'` 哨兵 + 精确比较。已杀 3 处（captcha gate/_file_visible_in_ui/cdp_pick_option_visually），新代码别再犯。
2. **evaluate 传裸箭头**：`()=>{}` 不是 IIFE `(()=>{})()`（会被双重调用 → 静默 []）。
3. **agent worktree 基座**：必须从 `feat/observe-act-generic` 当前 tip 开（P3-B/P3-D 都栽在 69da7078d 旧基座上）。
   验法：`git merge-base feat/observe-act-generic <sha>` 必须 = tip 附近。
4. **playground 全绿 ≠ live 全绿**：fixture 保真不足会绿在假靶上；每个 live 失败先 CDP 直连真页面定根因。
5. **并发 sweep 互杀**：两个 sweep 都 pkill chromium 会互相杀浏览器 → rerun 判决全废。开跑前 `pgrep -fl chromium` 查场。
6. **grouped-locate desync**：`located:grouped` 绑的代表节点 .value 会和真控件脱钩，别用于 already-correct 短路。
7. **`rendered_present` 对 radio 不安全**：选项文本永远在 DOM，radio 用 .checked/选中态确认。
8. **scorer 曾读死旧文件**报假 91.7% —— 打分永远显式传 results.json 路径。

## 7. 关闭路径（照此顺序推）

1. 收 f1/f2（fixture RED→GREEN + live 截图亲验）→ 我方复核（merge-base、静态模式扫描、selfcheck 137 不缩）→ 合入引擎分支
2. 派 F3（#13 spec 现成）→ 同标准验收
3. 收 P3-B/P3-D rebase 重验报告 → 合入
4. **28 行重跑**（15 假绿 + 13 NH-残余，rerun_failed 或手工 tsv）→ 再来一轮对抗审计 → 全绿才关 #5
5. P5：7 类剩余 fixture 挖真 DOM 进门禁 → selfcheck 全绿 → 闭环测试（新 sweep 挖不出新类）
6. FINAL：全新 500 URL fresh sweep（`fetch_fresh_urls` 工具在 runs/newats）→ 全量对抗审计 → 正式数字（审计后 ≥90%）
7. 每步都更新任务面板 + 不许在引擎自报上关任何任务

## 8. 边界（别碰）

- `ghosthands/` 目录 = worker-fill session 的地盘（分支 feat/observe-act-worker-fill）。我们只动 `experiments/jobapply-core`。
- Workday 是独立战线（wd_one/run_wizard 驱动 + 账号门），不在本战役范围。
- 凭据策略：随机邮箱 + `Ryan12310!`，遇 verify-code 直接 bail，**永不真提交**（live 测试停在 submit 前）。
