import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  useEffect(() => {
    api.会话().then(setList)
  }, [])
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>会话册</h2>
      <p className="lead">最近会话列表；点开可看脉冲与事件流水。</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => api.会话详情(s.id).then(setDetail)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
            {!detail.重放核对?.一致 && (
              <button
                type="button"
                style={{ marginLeft: 12 }}
                onClick={() =>
                  api.修复(detail.id).then(() => api.会话详情(detail.id)).then(setDetail)
                }
              >
                按争用结局修复
              </button>
            )}
          </h3>
          <p>
            锁定电价 {detail.price_locked} · 电量 {detail.kwh} · 金额 {detail.amount} ·
            关账世代 {detail.settle_generation} · 在途计数 {detail.inflight_count}
          </p>
          {detail.重放核对 && (
            <p className={`msg ${detail.重放核对.一致 ? 'ok' : ''}`}>
              重放核对：{detail.重放核对.一致 ? '一致' : `不一致 → ${detail.重放核对.偏差.join('；')}`}
              （重算电量 {detail.重放核对.重算电量} / 重算金额 {detail.重放核对.重算金额} /
              在途未收口 {detail.重放核对.在途未收口} / 桩位 {detail.重放核对.桩位状态} /
              枪线 {detail.重放核对.枪线状态}）
            </p>
          )}
          <h3>在途脉冲登记</h3>
          <table>
            <thead>
              <tr>
                <th>令牌</th>
                <th>登记时间</th>
                <th>电量</th>
                <th>状态</th>
                <th>屏障世代</th>
                <th>对应电量行</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.在途脉冲 || []).map((f) => (
                <tr key={f.id}>
                  <td>{f.token.slice(0, 8)}</td>
                  <td>{f.registered_at}</td>
                  <td>{f.delta_kwh}</td>
                  <td>{f.status}</td>
                  <td>{f.settle_generation}</td>
                  <td>{f.pulse_row_id ?? '—'}</td>
                  <td>{f.note}</td>
                </tr>
              ))}
              {!(detail.在途脉冲 || []).length && (
                <tr><td colSpan={7}>无在途登记</td></tr>
              )}
            </tbody>
          </table>
          <h3>脉冲</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲 || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
