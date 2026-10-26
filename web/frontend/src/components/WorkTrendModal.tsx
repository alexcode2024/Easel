import { useCallback, useEffect, useState } from 'react';
import {
  fetchWorkTrend, fetchMonitorStatus, runMonitorNow, saveMonitorSchedule, toggleWorkMonitor,
} from '../lib/api';
import type { BenchTrend, BenchMonitorStatus } from '../lib/api';
import { fmtDate, fmtDay, fmtValue, fmtWhen, pad2 } from '../lib/trendFormat';
import TrendChart from './TrendChart';

/** 每条曲线各自的颜色（浅色/深色主题下都够对比度）。 */
const SERIES_COLOR: Record<string, string> = {
  likes: '#e2603c',
  favorites: '#d98b1f',
  comments: '#3d7fd6',
  plays: '#7c5cd6',
};

interface WorkTrendModalProps {
  bid: string;
  workId: string;
  workTitle: string;
  onClose: () => void;
  /** 开关/采样变了之后通知外层重拉作品列表（作品卡上的开关要跟着变）。 */
  onChanged: () => void;
}

/** 单篇作品的表现趋势：点赞 / 收藏 / 评论各一条折线，最近 15 天。
 *
 *  数据来自「监控」的按日采样（`outputs/_benchmarks/<bid>/monitor.json`）：
 *  平台只给累计快照，没有历史接口，所以趋势只能自己一天记一笔。
 */
export default function WorkTrendModal({ bid, workId, workTitle, onClose, onChanged }: WorkTrendModalProps) {
  const [trend, setTrend] = useState<BenchTrend | null>(null);
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<BenchMonitorStatus | null>(null);
  const [poll, setPoll] = useState(false);

  const reload = useCallback((): Promise<BenchTrend | null> => (
    fetchWorkTrend(bid, workId)
      .then((data) => {
        setTrend(data);
        if (data.run?.running) setPoll(true);
        return data;
      })
      .catch((e) => {
        setErr(e instanceof Error ? e.message : '读取趋势失败');
        return null;
      })
  ), [bid, workId]);

  useEffect(() => { void reload(); }, [reload]);

  // 采样中轮询进度：一轮可能要几十秒（逐条详情），结束后自动重拉曲线
  useEffect(() => {
    if (!poll) return;
    const timer = window.setInterval(async () => {
      try {
        const st = await fetchMonitorStatus();
        setRun(st);
        if (!st.running) {
          setPoll(false);
          await reload();
          onChanged();
        }
      } catch { /* 轮询失败忽略，下一轮再试 */ }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [poll, reload, onChanged]);

  const toggle = async (enabled: boolean) => {
    setBusy(true); setErr(''); setMsg('');
    try {
      const r = await toggleWorkMonitor(bid, workId, enabled);
      setMsg(r.message || (enabled ? '已开启监控' : '已停止监控'));
      await reload();
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : '操作失败');
    } finally { setBusy(false); }
  };

  const sampleNow = async () => {
    setErr(''); setMsg('');
    try {
      const r = await runMonitorNow(bid);
      if (r.state === 'running') {
        setPoll(true);
        setMsg(r.message || '开始采样…');
      } else {
        setMsg(r.message || '没有需要采样的作品');
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '触发采样失败');
    }
  };

  const changeTime = async (value: string) => {
    const [h, m] = value.split(':').map((x) => Number(x));
    if (!Number.isFinite(h) || !Number.isFinite(m)) return;
    setErr(''); setMsg('');
    try {
      await saveMonitorSchedule(h, m);
      await reload();
      setMsg(`已改为每天 ${pad2(h)}:${pad2(m)} 采样`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : '改采样时间失败');
    }
  };

  const mon = trend?.monitor;
  const windowDays = trend?.window_days ?? 15;
  const status = mon?.enabled ? 'on' : mon?.expired ? 'expired' : 'off';
  const timeValue = trend ? `${pad2(trend.schedule.hour)}:${pad2(trend.schedule.minute)}` : '09:00';
  // 窗口内一个点都没有：三张空图并排只是白占地方，换成一块说明 + 入口
  const hasAny = (trend?.points.length ?? 0) > 0;

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal trend-modal" onClick={(e) => e.stopPropagation()}>
        <div className="trend-head">
          <div style={{ minWidth: 0 }}>
            <div className="trend-title" title={trend?.work.title || workTitle}>
              {trend?.work.title || workTitle}
            </div>
            <div className="trend-sub">
              {trend?.account.nickname || ''}
              {trend ? ` · ${trend.account.platform_name}` : ''}
              {trend?.work.url && (
                <a href={trend.work.url} target="_blank" rel="noreferrer">原文 ↗</a>
              )}
            </div>
          </div>
          <button className="icon-btn" onClick={onClose} title="关闭">×</button>
        </div>

        {!trend && !err && <div className="loading" style={{ padding: 40 }}><div className="spinner" />加载中…</div>}
        {err && <div className="bench-err" style={{ margin: '10px 0 0' }}>{err}</div>}

        {trend && (
          <>
            <div className={`trend-mon ${status}`}>
              <div className="trend-mon-row">
                <span className={`trend-pill ${status}`}>
                  {mon?.enabled ? '● 监控中' : mon?.expired ? '已到期' : '未开启'}
                </span>
                <span className="trend-mon-meta">
                  已记录 <b>{mon?.days ?? 0}</b>/{windowDays} 天
                  {mon?.started_at ? <span className="trend-dim"> · 起于 {fmtDate(mon.started_at)}</span> : null}
                  {mon?.end_day ? <span className="trend-dim"> · 到 {fmtDay(mon.end_day)}</span> : null}
                </span>
                <span className="trend-mon-actions">
                  <button className="btn btn-sm btn-primary" disabled={busy}
                    onClick={() => toggle(!mon?.enabled)}>
                    {busy ? '处理中…' : mon?.enabled ? '停止监控' : mon?.expired ? '重新开始 15 天' : '开启监控'}
                  </button>
                  <button className="btn btn-sm" onClick={sampleNow} disabled={run?.running}
                    title="马上采一次（会覆盖今天的点）">
                    {run?.running ? `采样中 ${run.done}/${run.total}` : '⟳ 立即采样'}
                  </button>
                </span>
              </div>
              <div className="trend-mon-row dim">
                <span>
                  每天
                  <input className="trend-time" type="time" value={timeValue}
                    onChange={(e) => changeTime(e.target.value)}
                    title="固定采样时间（本地时区）。改时间不影响已记录的点" />
                  自动采样 · 下次 {fmtWhen(trend.next_run_ts)}
                </span>
                <span className="trend-dim">{run?.running ? run.message : (msg || '')}</span>
              </div>
            </div>

            {(mon?.last_error || run?.state === 'risk') && (
              <div className="trend-warn">
                最近一次采样有问题：{mon?.last_error || run?.message}
                <br />
                命中平台风控时会立即停手、不重试；已完成的部分都已落盘，风控解除后点「立即采样」补一次即可。
              </div>
            )}

            {!hasAny && (
              <div className="trend-empty">
                <div className="trend-empty-icon">📈</div>
                <div className="trend-empty-title">最近 {windowDays} 天还没有采样数据</div>
                <p className="trend-empty-text">
                  开启监控后，每天 {pad2(trend.schedule.hour)}:{pad2(trend.schedule.minute)}
                  （本地时间，可改）自动记一次{trend.series.map((s) => s.label).join('/')}；
                  也可以现在采一次作为起点。
                  <br />
                  平台只给累计快照、没有历史接口，所以这里是自己一天记一笔，最多连续记 {windowDays} 天。
                </p>
                <div className="trend-empty-actions">
                  {!mon?.enabled && (
                    <button className="btn btn-sm btn-primary" disabled={busy}
                      onClick={() => toggle(true)}>
                      {busy ? '处理中…' : '开启监控'}
                    </button>
                  )}
                  <button className="btn btn-sm" onClick={sampleNow} disabled={run?.running}>
                    {run?.running ? `采样中 ${run.done}/${run.total}` : '⟳ 立即采样'}
                  </button>
                </div>
              </div>
            )}

            {hasAny && trend.series.map((s) => {
              const { delta, pct, count, last } = s.stats;
              const cls = delta === null || delta === 0 ? '' : delta > 0 ? 'up' : 'down';
              return (
                <section key={s.key} className="trend-block">
                  <div className="trend-block-head">
                    <span className="trend-block-name">
                      <i className="trend-dot" style={{ background: SERIES_COLOR[s.key] }} />
                      {s.label}
                    </span>
                    {s.available ? (
                      <span className="trend-block-num">
                        <b>{fmtValue(last)}</b>
                        {count > 1 && delta !== null && (
                          <span className={`trend-delta ${cls}`}>
                            {delta > 0 ? '+' : ''}{fmtValue(delta)}
                            {pct !== null ? `（${pct > 0 ? '+' : ''}${pct}%）` : ''}
                          </span>
                        )}
                        {count === 0 && <span className="trend-dim">这几天没采到</span>}
                      </span>
                    ) : (
                      <span className="trend-block-note">该平台不公开{s.label}数</span>
                    )}
                  </div>
                  {!s.available ? (
                    <div className="trend-unavailable">
                      这个指标平台不公开，无法监控——不做估算，也不用别的字段顶替。
                    </div>
                  ) : count === 0 ? (
                    <div className="trend-unavailable">
                      这几天没采到{s.label}数（没读到的日子留空，不补 0、不猜）。
                    </div>
                  ) : (
                    <TrendChart rows={s.rows} color={SERIES_COLOR[s.key]} today={trend.today} />
                  )}
                </section>
              );
            })}

            <p className="trend-foot">
              平台只给累计快照，所以这里是自己按天记的：每天到点采一次，一天一条；
              本地服务没在跑时，下次打开会补当天的这一次。满 {windowDays} 天自动停止，已记录的点保留。
              没采到的日子不画点、也不插值；真实观测值之间照常连成折线，跨过空白的那段用虚线标出。
              {mon?.expired && ' 这一轮已记满 15 天，想继续就点「重新开始 15 天」。'}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
