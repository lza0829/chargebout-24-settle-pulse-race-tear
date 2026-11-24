# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。

## 关账 × 脉冲 争用口径（全局唯一）

本系统只贯彻 **口径 B：在途脉冲优先，完成后再关**，禁止与“关账优先”混用。

- **脉冲两段式**：先在 `inflight_pulses` 登记可见在途（`inflight_count+1`），再 `BEGIN IMMEDIATE`
  复查落账。屏障之前登记的在途允许落账；屏障之后或会话已离充的，整笔拒绝——不留 `pulses` 行、
  不推进累计电量/金额。
- **关账两段式**：先把“充电中”推进“结算中”并令 `settle_generation+1`（关账屏障）；轮询等待该会话
  全部在途收口（完成或超时清扫）后，才在同一写事务内同刷 状态=已关账 / 金额 / 桩枪释放 / 快照。
  等待超时则整笔失败、会话停留“结算中”可重试，**不留半截**。
- **同刷与双桶**：关账态、桩枪空闲态、累计电量、金额、会话详情、总览在同一事务落定；总览始终如实
  展示在途脉冲数/电量，关账完成前绝不假装零在途。
- **可重放/可修复**：`contention_journal` 留存关账屏障与临界前快照；`/api/会话/{id}/重放` 以 `pulses`
  行为真值重算并核对状态机与占用；`/api/会话/{id}/修复` 把任何半截（如桩已释放仍充电中、回执成功却无
  电量行）对齐回争用结局。
- **并发底座**：SQLite 启用 WAL，争用写连接用 autocommit + 显式 `BEGIN IMMEDIATE` 串行化，并对
  锁升级式 `SQLITE_BUSY` 做有界重试。

测试：`cd backend && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v`
（夹具覆盖：关账抢先脉冲被拒、脉冲先进关账带增量、半截占用/状态分裂修复、回执成功无电量行、
在途超时清扫、关账等待超时、真实多连接并发压测。）零电量关账策略另立需求，不在本范围内。
