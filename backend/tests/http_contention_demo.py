"""真实 HTTP 并发争用验证（口径B）。对运行中的 uvicorn 打并发请求。"""
import json
import threading
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://127.0.0.1:11599/api"


def enc(path: str) -> str:
    # 仅对路径中的中文做百分号编码，保留 / 与数字
    return urllib.parse.quote(path, safe="/")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + enc(path), data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def open_session(pile):
    _, r = call("POST", "/开会话", {"桩位编号": pile})
    return r["会话编号"]


def scenario_pulse_first(pile):
    sid = open_session(pile)
    outcomes = []

    def pulse():
        st, r = call("POST", f"/会话/{sid}/脉冲", {"电量": 4, "功率": 7})
        outcomes.append(("脉冲", st, r))

    # 先启动脉冲并稍等，让其登记在途；再关账。关账必须等其收口并带上 4 度
    t = threading.Thread(target=pulse)
    t.start()
    threading.Event().wait(0.05)
    st, settle_r = call("POST", f"/会话/{sid}/结算", {})
    t.join()
    _, detail = call("GET", f"/会话/{sid}")
    print(f"[脉冲先进] 关账HTTP={st} 电量={settle_r.get('电量')} 状态={detail['status']} "
          f"脉冲行={len(detail['脉冲'])} 在途={detail['重放核对']['在途未收口']} "
          f"一致={detail['重放核对']['一致']} 桩位={detail['重放核对']['桩位状态']}")
    assert detail["status"] == "已关账"
    assert abs(detail["kwh"] - 4.0) < 1e-9, "关账必须带上在途脉冲增量"
    assert detail["重放核对"]["一致"]
    assert detail["重放核对"]["桩位状态"] == "空闲"


def scenario_settle_first(pile):
    sid = open_session(pile)
    call("POST", f"/会话/{sid}/脉冲", {"电量": 1, "功率": 7})  # 先入账 1 度
    # 直接关账成功；随后立刻并发多笔脉冲，全部应被 409 拒绝且不留行
    st, settle_r = call("POST", f"/会话/{sid}/结算", {})
    assert st == 200
    rejections = []

    def late_pulse():
        c, r = call("POST", f"/会话/{sid}/脉冲", {"电量": 5, "功率": 7})
        rejections.append((c, r))

    ts = [threading.Thread(target=late_pulse) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    _, detail = call("GET", f"/会话/{sid}")
    codes = {c for c, _ in rejections}
    print(f"[关账抢先] 晚到脉冲HTTP码={codes} 被拒数={len(rejections)} "
          f"账面电量={detail['kwh']} 脉冲行={len(detail['脉冲'])} 状态={detail['status']}")
    assert codes == {409}
    assert abs(detail["kwh"] - 1.0) < 1e-9, "被拒脉冲不得推进累计"
    assert len(detail["脉冲"]) == 1, "被拒脉冲不得留 pulses 行"
    assert detail["status"] == "已关账"


if __name__ == "__main__":
    # 用两个不同空闲桩，避免两个场景相互占用
    scenario_pulse_first(2)
    scenario_settle_first(3)
    print("HTTP 并发争用：两种结局均符合口径B")
