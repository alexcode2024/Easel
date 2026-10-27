import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  fetchBenchPlatforms, fetchBenchmarks, fetchBenchmark, createBenchmark, deleteBenchmark,
  fetchBenchmarkNow, fetchBenchmarkWorkDetail, fetchBenchmarkWorksDetailBatch,
  fetchAllBenchmarks, benchmarkFetchAllStatus,
  fetchPlatformAccessStatus,
  fetchBenchCookies, downloadBenchWork,
  fetchMonitorSchedule, fetchMonitorStatus, runMonitorNow, toggleWorkMonitor,
  mediaUrl, createIdea,
} from '../lib/api';
import type {
  BenchAccount, BenchDetail, BenchPlatform, BenchWork, BenchCookieStatus, BenchMonitorSchedule,
  PlatformAccessStatus,
} from '../lib/api';
import CookieModal from './CookieModal';
import WorkTrendModal from './WorkTrendModal';
import PlatformAccessBanner from './PlatformAccessBanner';
import { fmtDateTime, fmtWhen, pad2 } from '../lib/trendFormat';
import { IconAccounts, IconTrash, IconChevron, IconChat } from './icons';

/** 对标账号：管理「别人的」公开账号，看他们最近在发什么、哪些是爆款。
 *
 *  与「自有账号」的区别：这里的数据全部来自平台**公开**信息，
 *  平台不公开的字段（如小红书的累计收藏/评论）一律显示「—」，
 *  不估算、不编造——这是仓库既定红线（skill-competitor-analysis/references/data-collection.md）。
 */
interface BenchmarksPageProps {
  onAskAgent: (prompt: string) => void;
  onBreakdown?: (text: string) => void;
}

const fmtNum = (n: number | null | undefined): string => {
  if (n === null || n === undefined) return '—';
  if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return String(n);
};

/** 指标展示：平台给的展示串（如「1万+」）原样保留；
 *  平台给精确数字时（TikTok/抖音返回整数）按中文习惯缩写，避免出现「96300000」。 */
function metricText(m: { raw: string; num: number | null; lower_bound: boolean } | null | undefined): string {
  if (!m || (m.num === null && !m.raw)) return '—';
  const raw = (m.raw || '').trim();
  const pureNumber = /^[\d.,\s]+$/.test(raw);
  if (raw && !pureNumber) return raw;
  return m.num === null ? raw : fmtNum(m.num);
}

function timeAgo(ms: number | null | undefined): string {
  if (!ms) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const diff = Date.now() - d.getTime();
  if (diff < 0) return d.toLocaleDateString('zh-CN');
  const day = 86400000;
  if (diff < 3600000) return `${Math.max(1, Math.floor(diff / 60000))} 分钟前`;
  if (diff < day) return `${Math.floor(diff / 3600000)} 小时前`;
  if (diff < day * 30) return `${Math.floor(diff / day)} 天前`;
  return d.toLocaleDateString('zh-CN');
}

export default function BenchmarksPage({ onAskAgent, onBreakdown }: BenchmarksPageProps) {
  const [accounts, setAccounts] = useState<BenchAccount[]>([]);
  const [platforms, setPlatforms] = useState<BenchPlatform[]>([]);
  const [detail, setDetail] = useState<BenchDetail | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [form, setForm] = useState<{ platform: string; target: string; remark: string } | null>(null);
  const [toast, setToast] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [bulk, setBulk] = useState<{ running: boolean; done: number; total: number } | null>(null);
  const [showAllWorks, setShowAllWorks] = useState(false);
  const [busyWork, setBusyWork] = useState('');
  const [syncBudget, setSyncBudget] = useState(0);
  const [cookies, setCookies] = useState<BenchCookieStatus[]>([]);
  const [platformAccessError, setPlatformAccessError] = useState('');
  const [platformAccess, setPlatformAccess] = useState<Record<string, PlatformAccessStatus> | null>(null);
  const [cookieForm, setCookieForm] = useState<{ platform: string; value: string } | null>(null);
  // 表现监控：设置（每天几点采样）+ 正在跑的那一轮进度；趋势弹窗针对单篇作品
  const [monitorInfo, setMonitorInfo] = useState<BenchMonitorSchedule | null>(null);
  const [sampling, setSampling] = useState<{ running: boolean; done: number; total: number } | null>(null);
  const [trendWork, setTrendWork] = useState<BenchWork | null>(null);
  // 作品行的「⋯」菜单：一次只开一个，点空白处/按 Esc 关闭
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  // 冷却倒计时：后端只在响应里给「还剩多少秒」，不会自己走；这里记下列表拉取时刻本地递减，
  // 否则抓完一次后行内刷新按钮会一直灰着直到刷新页面。
  const listAtRef = useRef(Date.now());
  const [, setTick] = useState(0);

  const showToast = (m: string) => { setToast(m); setTimeout(() => setToast(''), 2600); };

  const cooldownLeft = useCallback((a: BenchAccount): number => {
    const base = a.cooldown_left ?? 0;
    if (base <= 0) return 0;
    return Math.max(0, Math.round(base - (Date.now() - listAtRef.current) / 1000));
  }, []);

  const loadList = useCallback(() => {
    listAtRef.current = Date.now();
    fetchBenchmarks().then(setAccounts).catch((e) => setErr(String(e.message || e)));
  }, []);

  const loadPlatformAccess = useCallback(() => {
    fetchPlatformAccessStatus().then((statuses) => {
      setPlatformAccess(statuses);
      setPlatformAccessError('');
    }).catch(() => setPlatformAccessError('状态读取失败，正在重试…'));
  }, []);

  const loadDetail = useCallback((id: string) => {
    fetchBenchmark(id).then(setDetail).catch((e) => setErr(String(e.message || e)));
  }, []);

  // 监控设置（全局：每天几点采样）——页头的「监控 N 篇 · 每天 09:00」用它渲染
  const loadMonitorInfo = useCallback(() => {
    fetchMonitorSchedule()
      .then((info) => {
        setMonitorInfo(info);
        if (info.running) {
          setSampling((prev) => (prev?.running ? prev : {
            running: true, done: info.last_run.done, total: info.last_run.total,
          }));
        }
      })
      .catch(() => { /* 设置读不到不影响主流程 */ });
  }, []);

  useEffect(() => { loadMonitorInfo(); }, [loadMonitorInfo]);
  useEffect(() => {
    loadPlatformAccess();
    const timer = window.setInterval(loadPlatformAccess, 3000);
    return () => window.clearInterval(timer);
  }, [loadPlatformAccess]);

  // 作品行的 「⋯」菜单：Esc 关闭（点空白由遮罩负责）
  useEffect(() => {
    if (!openMenu) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpenMenu(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openMenu]);

  // 监控状态变了 → 作品卡（开关/天数）与页头汇总一起刷
  const refreshMonitorViews = useCallback(() => {
    if (selected) loadDetail(selected);
    loadMonitorInfo();
  }, [selected, loadDetail, loadMonitorInfo]);

  // 采样在跑时轮询：一轮几十秒到几分钟（逐条详情），结束后把作品列表刷新一遍
  useEffect(() => {
    if (!sampling?.running) return;
    const tick = window.setInterval(async () => {
      try {
        const st = await fetchMonitorStatus();
        if (st.running) {
          setSampling({ running: true, done: st.done, total: st.total });
          return;
        }
        setSampling({ running: false, done: st.done, total: st.total });
        refreshMonitorViews();
      } catch { /* 轮询失败忽略，下轮再试 */ }
    }, 3000);
    return () => window.clearInterval(tick);
  }, [sampling?.running, refreshMonitorViews]);

  useEffect(() => {
    fetchBenchPlatforms().then((r) => setPlatforms(r.platforms)).catch(() => {});
    fetchBenchCookies().then(setCookies).catch(() => {});
    loadList();
  }, [loadList]);

  useEffect(() => {
    if (selected) { setShowAllWorks(false); loadDetail(selected); }
    else setDetail(null);
  }, [selected, loadDetail]);

  // 抓取中轮询：抓一次 30s～几分钟，必须异步 + 轮询（同步请求会被代理超时掐断）
  useEffect(() => {
    const running = accounts.some((a) => a.running || a.fetch?.state === 'running')
      || (bulk?.running ?? false);
    if (!running) {
      if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const list = await fetchBenchmarks();
        listAtRef.current = Date.now();
        setAccounts(list);
        if (selected) loadDetail(selected);
        const b = await benchmarkFetchAllStatus();
        setBulk((prev) => (prev ? { ...prev, running: b.running, done: b.done, total: b.total } : prev));
        loadPlatformAccess();
      } catch { /* 轮询失败忽略，下轮再试 */ }
    }, 3000);
    return () => {
      if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
    };
  }, [accounts, bulk, selected, loadDetail, loadPlatformAccess]);

  const platMeta = useMemo(() => {
    const map: Record<string, BenchPlatform> = {};
    for (const p of platforms) map[p.platform] = p;
    return map;
  }, [platforms]);

  // 有账号在冷却中时每秒重绘一次，让「冷却 4:32」真的在走
  useEffect(() => {
    const anyCooling = accounts.some((a) => (a.cooldown_left ?? 0) > 0);
    if (!anyCooling) return;
    const t = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [accounts]);

  const openNew = () => {
    const first = platforms.find((p) => p.auto) || platforms[0];
    setForm({ platform: first?.platform || 'xiaohongshu', target: '', remark: '' });
  };

  const submit = async () => {
    if (!form) return;
    setBusy(true); setErr('');
    try {
      const acc = await createBenchmark({ platform: form.platform, target: form.target, remark: form.remark });
      setForm(null);
      loadList();
      showToast('已添加');
      if (platMeta[acc.platform]?.auto) {
        // 添加后直接抓一次，省得用户再点一下
        await fetchBenchmarkNow(acc.id).catch(() => {});
        loadList();
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '添加失败');
    } finally { setBusy(false); }
  };

  const refresh = async (acc: BenchAccount) => {
    setErr('');
    try {
      await fetchBenchmarkNow(acc.id, syncBudget);
      loadList();
      if (selected === acc.id) loadDetail(acc.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : '抓取失败');
    }
  };

  const remove = async (acc: BenchAccount) => {
    if (!window.confirm(`删除对标账号「${acc.nickname || acc.account_id}」？抓取到的数据和封面缓存会一并删除。`)) return;
    await deleteBenchmark(acc.id);
    if (selected === acc.id) setSelected(null);
    loadList();
  };

  const refreshAll = async () => {
    setErr('');
    try {
      const r = await fetchAllBenchmarks();
      setBulk({ running: true, done: 0, total: r.total });
      showToast(`开始串行刷新 ${r.total} 个账号（不并发，避免账号被降级）`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : '批量刷新失败');
    }
  };

  const toIdeas = async (w: BenchWork, acc: BenchAccount) => {
    await createIdea({
      title: w.title.slice(0, 60),
      note: `来源：对标账号「${acc.nickname || acc.account_id}」（${acc.platform_name}）\n`
        + `点赞 ${w.likes ?? '—'}｜收藏 ${w.favorites ?? '—'}｜评论 ${w.comments ?? '—'}\n`
        + `${w.url || '（平台未返回作品链接）'}`,
      source: '对标账号',
      status: 'pending',
    });
    showToast('已存入选题库');
  };

  const deepDive = async (w: BenchWork) => {
    if (!selected) return;
    try {
      await fetchBenchmarkWorkDetail(selected, w.id);
      loadDetail(selected);
      showToast('已深挖该条数据');
    } catch (e) {
      setErr(e instanceof Error ? e.message : '深挖失败');
    }
  };

  // 下载这条笔记的全部数据：元数据 + 图片原图（含动图）+ 视频（默认 1080p）。
  // 链接不用粘贴——作品列表里已经有 note_id 与 xsec_token。
  const downloadWork = async (w: BenchWork) => {
    if (!selected) return;
    setErr('');
    setBusyWork(w.id);
    try {
      const r = await downloadBenchWork(selected, w.id);
      showToast(r.message || '开始下载…');
      loadDetail(selected);            // 状态由后端写入，轮询会带出进度
    } catch (e) {
      setErr(e instanceof Error ? e.message : '下载失败');
    } finally {
      setTimeout(() => setBusyWork(''), 4000);
    }
  };

  // 批量补齐收藏/评论：逐条打开作品页（串行 + 间隔，风控即停），进度靠已有轮询刷新
  const deepDiveBatch = async () => {
    if (!selected) return;
    setErr('');
    try {
      const r = await fetchBenchmarkWorksDetailBatch(selected, 0);   // 0 = 把待补的全部补完
      if (r.state === 'done') { showToast(r.message || '都已补齐'); loadDetail(selected); return; }
      loadDetail(selected);
      showToast(`开始逐条读取 ${r.total ?? ''} 条作品（每条约 4-7 秒；中途停手已完成的部分会保留）`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : '批量深挖失败');
    }
  };

  const autoPlatforms = platforms.filter((p) => p.auto);
  const manualPlatforms = platforms.filter((p) => !p.auto);

  // 单篇作品开/关监控：开启时后端会顺手记下今天的第一个点（列表型平台除外）
  const toggleMonitor = async (w: BenchWork) => {
    if (!selected || !w.id) return;
    const on = !!w.monitor?.enabled;
    setErr('');
    setBusyWork(w.id);
    try {
      const r = await toggleWorkMonitor(selected, w.id, !on);
      showToast(r.message || (on ? '已停止监控' : '已开启监控'));
      refreshMonitorViews();
    } catch (e) {
      setErr(e instanceof Error ? e.message : '操作失败');
    } finally {
      setTimeout(() => setBusyWork(''), 600);
    }
  };

  const sampleAllNow = async () => {
    if (!selected) return;
    setErr('');
    try {
      const r = await runMonitorNow(selected);
      if (r.state === 'running') {
        setSampling({ running: true, done: 0, total: r.total || 0 });
        showToast(r.message || '开始采样');
      } else {
        showToast(r.message || '没有需要采样的作品');
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '触发采样失败');
    }
  };

  // 该账号下监控的概况（作品卡上的开关状态总和）
  const monStats = useMemo(() => {
    let enabled = 0;
    let expired = 0;
    for (const w of detail?.works ?? []) {
      if (w.monitor?.enabled) enabled += 1;
      else if (w.monitor?.expired) expired += 1;
    }
    return { enabled, expired };
  }, [detail]);

  return (
    <div className="page-scroll bench-page">
      <div className="page-head">
        <div>
          <h1 className="page-title"><IconAccounts size={21} /> 对标账号</h1>
          <p className="page-subtitle">
            盯住同赛道账号在发什么、哪条跑出来了。数据来自平台<strong>公开</strong>信息，
            平台不公开的字段显示「—」，不估算。
          </p>
          <PlatformAccessBanner statuses={platformAccess} showIdle error={platformAccessError} />
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {selected && <button className="btn btn-sm" onClick={() => setSelected(null)}>← 返回列表</button>}
          {!selected && accounts.some((a) => platMeta[a.platform]?.auto) && (
            <button className="btn btn-sm" onClick={refreshAll} disabled={bulk?.running}>
              {bulk?.running ? `刷新中 ${bulk.done}/${bulk.total}` : '⟳ 全部刷新'}
            </button>
          )}
          {!selected && (
            <button className="btn btn-sm" title="粘贴浏览器里的 cookie：小红书可据此走纯 HTTP 取数（不用浏览器）"
              onClick={() => {
                const first = cookies[0];
                setCookieForm({ platform: first?.platform || 'xiaohongshu', value: '' });
              }}>
              🔑 Cookie
              {cookies.some((c) => c.saved) && <span className="bench-tag ok" style={{ marginLeft: 5 }}>已配置</span>}
            </button>
          )}
          {!selected && <button className="btn btn-sm btn-primary" onClick={openNew}>+ 添加对标账号</button>}
          {selected && detail && (
            <>
              {(() => {
                // 判定以「有没有数据」为准，而不是 detail_fetched：早先版本会在没读到
                // 数据时也标完成，那些残留标记不能让它永远不再被深挖（后端同样自愈）。
                const pending = detail.works.filter((w) => w.id && !(
                  w.detail_fetched && (w.favorites !== null || w.comments !== null))).length;
                if (pending === 0) return null;
                const batchSize = Math.min(pending, 20);
                const mins = Math.max(1, Math.ceil(batchSize * 18 / 60));
                return (
                  <button className="btn btn-sm" onClick={deepDiveBatch}
                    disabled={detail.account.running || detail.fetch?.state === 'running'}
                    title={`逐条读取收藏/评论：本轮最多 ${batchSize} 条，约 ${mins} 分钟；实际耗时受排队和网络影响。` +
                           `串行不并发；命中平台风控会立即停止，已完成的部分会保留。`}>
                    ⟳ 补齐收藏/评论（本轮 {batchSize} 条，约 {mins} 分钟）
                  </button>
                );
              })()}
              {detail.account.platform === 'xiaohongshu' && (
                <label className="bench-budget"
                  title="刷新时同步本地素材：没下载过的补下素材（图文约 1～17MB；视频按最低画质，约 5～35MB），已下载的只更新点赞/收藏/评论">
                  同步下载
                  <select value={syncBudget} onChange={(e) => setSyncBudget(Number(e.target.value))}>
                    <option value={0}>仅刷新列表（默认）</option>
                    <option value={3}>最多 3 条</option>
                    <option value={5}>最多 5 条</option>
                  </select>
                </label>
              )}
              <button className="btn btn-sm btn-primary"
                onClick={() => refresh(detail.account)}
                disabled={detail.account.running || (detail.cooldown_left ?? 0) > 0}
                title="重新抓主页列表；选择同步预算后才会读取详情和下载素材">
                {detail.account.running
                  ? '抓取中…'
                  : (detail.cooldown_left ?? 0) > 0 ? `冷却 ${detail.cooldown_left}s` : syncBudget > 0 ? '⟳ 获取最新作品并按预算同步' : '⟳ 获取最新作品'}
              </button>
            </>
          )}
        </div>
      </div>

      {err && <div className="bench-err">{err}</div>}

      {/* ---------------- 列表视图 ---------------- */}
      {!selected && (
        <>
          {accounts.length === 0 && (
            <div className="card bench-empty">
              <div style={{ fontSize: 30 }}>🎯</div>
              <h3 style={{ margin: '10px 0 6px' }}>还没有对标账号</h3>
              <p style={{ color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.7, margin: 0 }}>
                {autoPlatforms.length > 0
                  ? `当前可自动抓取：${autoPlatforms.map((p) => p.name).join('、')}。`
                  : '当前没有可自动抓取的平台，可先手动登记账号。'}<br />
                添加后会自动抓一次，之后随时点「获取最新作品」更新。
              </p>
              <button className="btn btn-sm btn-primary" style={{ marginTop: 14 }} onClick={openNew}>
                + 添加对标账号
              </button>
            </div>
          )}

          {accounts.length > 0 && (
            <div className="panel bench-table-wrap">
              <table className="bench-table">
                <thead>
                  <tr>
                    <th>平台</th>
                    <th>账号</th>
                    <th className="num">粉丝</th>
                    <th className="num">累计获赞</th>
                    <th className="num">累计收藏</th>
                    <th className="num">累计评论</th>
                    <th className="num">作品</th>
                    <th>最近更新</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {accounts.map((a) => {
                    const meta = platMeta[a.platform];
                    const st = a.fetch?.state || (a.running ? 'running' : 'idle');
                    return (
                      <tr key={a.id} className="bench-row" onClick={() => setSelected(a.id)}>
                        <td><span className="badge">{a.platform_name || meta?.name || a.platform}</span></td>
                        <td>
                          <div className="bench-name">
                            {a.nickname || <span className="bench-muted">（未抓取）</span>}
                            {meta?.experimental && <span className="bench-tag warn">未实测</span>}
                          </div>
                          <div className="bench-sub">{a.account_id}{a.remark ? ` · ${a.remark}` : ''}</div>
                        </td>
                        <td className="num">{metricText(a.followers)}</td>
                        <td className="num">{metricText(a.likes)}</td>
                        <td className="num">{metricText(a.favorites)}</td>
                        <td className="num">{metricText(a.comments)}</td>
                        <td className="num">{a.work_count || '—'}</td>
                        <td>
                          <div className="bench-sub">
                            {st === 'running' ? '抓取中…' : a.fetched_at ? timeAgo(a.fetched_at * 1000) : '未抓取'}
                          </div>
                          {st === 'risk' && <div className="bench-tag danger">风控熔断</div>}
                          {st === 'error' && <div className="bench-tag danger" title={a.last_error}>失败</div>}
                          {a.status === 'manual' && <div className="bench-tag">手动登记</div>}
                          {a.logged_in === false && <div className="bench-tag warn" title="登录态未生效，部分字段平台只给占位值">登录态失效</div>}
                          {(a.monitor?.monitored ?? 0) > 0 && (
                            <div className="bench-tag ok" title="这个账号下有作品在监控中，每天自动记录表现">
                              📈 监控 {a.monitor?.monitored}
                            </div>
                          )}
                        </td>
                        <td className="bench-actions" onClick={(e) => e.stopPropagation()}>
                          {meta?.auto && (() => {
                            const cd = cooldownLeft(a);
                            const running = a.running || a.fetch?.state === 'running';
                            return (
                              <button
                                className="bench-refresh"
                                disabled={running || cd > 0}
                                title={cd > 0
                                  ? `冷却中：读操作也会触发平台风控，约 ${cd} 秒后可再抓`
                                  : '获取该账号的最新作品'}
                                onClick={() => refresh(a)}
                              >
                                {running ? '抓取中…' : cd > 0 ? `冷却 ${cd}s` : '⟳ 刷新'}
                              </button>
                            );
                          })()}
                          <button className="session-act danger" title="删除" onClick={() => remove(a)}>
                            <IconTrash size={13} />
                          </button>
                          <IconChevron size={13} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {autoPlatforms.length > 0 && (
            <p className="bench-foot">
              抓取走平台公开信息、串行节流（同一账号两次抓取至少间隔 5 分钟）；
              命中平台风控会立即停止而<strong>不重试</strong>——重试会把软限流升级成数小时封禁。
            </p>
          )}
        </>
      )}

      {/* ---------------- 详情视图 ---------------- */}
      {selected && detail && (
        <>
          <div className="card bench-profile">
            <div className="bench-profile-main">
              <div className="bench-profile-name">
                {detail.account.nickname || detail.account.account_id}
                {detail.platform && (detail.platform as BenchPlatform).experimental && (
                  <span className="bench-tag warn">未实测路径</span>
                )}
              </div>
              {detail.account.bio && <div className="bench-sub">{detail.account.bio}</div>}
              <div className="bench-profile-meta">
                <span>{(detail.platform as BenchPlatform)?.name || detail.account.platform_name}</span>
                <span className="bench-dot">·</span>
                <span>{detail.account.account_id}</span>
                {detail.account.home_url && (
                  <>
                    <span className="bench-dot">·</span>
                    <a href={detail.account.home_url} target="_blank" rel="noreferrer">打开主页 ↗</a>
                  </>
                )}
                <span className="bench-dot">·</span>
                <button className="bench-act" onClick={() => {
                  const top = [...detail.works].sort((a, b) => (b.likes ?? 0) - (a.likes ?? 0)).slice(0, 5);
                  const brief = top.map((w, i) => `${i + 1}. ${w.title}（点赞 ${w.likes ?? '—'}）`).join('\n');
                  onAskAgent(
                    `这是我关注的对标账号「${detail.account.nickname || detail.account.account_id}」`
                    + `（${detail.account.platform_name}，粉丝 ${metricText(detail.account.followers)}）。\n`
                    + `它近期点赞最高的几条内容：\n${brief || '（暂无作品数据，请先抓取）'}\n\n`
                    + `请按竞品拆解的方法论分析：选题分布、格式偏好、爆款规律，`
                    + `并给出我（当前画像）可以差异化跟进的方向。数据来自公开主页，`
                    + `平台不公开的字段请如实标注，不要编造。`);
                }}><IconChat size={12} /> 让 Agent 分析</button>
              </div>
            </div>
            <div className="bench-profile-stats">
              {([['粉丝', detail.account.followers], ['累计获赞', detail.account.likes],
                 ['累计收藏', detail.account.favorites], ['累计评论', detail.account.comments]] as const).map(
                ([label, m]) => (
                  <div key={label} className="bench-stat">
                    <div className="bench-stat-val">{metricText(m)}</div>
                    <div className="bench-stat-label">{label}</div>
                  </div>
                ))}
            </div>
          </div>

          {(detail.coverage || detail.source || detail.median_likes) && (
            <div className="bench-note">
              数据来源：{detail.source || '平台公开信息'}
              {detail.coverage ? `（${detail.coverage}）` : ''}
              {detail.fetched_at ? ` · 更新于 ${timeAgo(detail.fetched_at * 1000)}` : ''}
              {detail.median_likes ? ` · 本账号点赞中位数 ${fmtNum(detail.median_likes)}（爆款线 = 中位数 × 3）` : ''}
            </div>
          )}

          {(detail.account.warnings || []).map((w, i) => (
            <div key={i} className={`bench-note ${/游客态|登录态/.test(w) ? 'alert' : 'dim'}`}>{w}</div>
          ))}

          {detail.account.logged_in === false && (
            <div className="bench-err">
              <strong>小红书 cookie 已失效或被平台降级（当前被判游客态）</strong>：粉丝数只给「1万+」这类
              占位值（已留空，不显示错的数字），作品列表也拿不到作品 ID（无原文链接、无法深挖）。
              累计获赞/收藏来自精确字段，仍然有效。
              <br />
              处理办法：在自己浏览器里登录小红书 → 复制新的 Cookie →
              点本页右上角「🔑 Cookie」粘贴保存，再点「获取最新作品」。
              若短时间请求偏多，平台会把会话降级数小时，等一会再试即可（本工具不绕过平台风控）。
            </div>
          )}

          {detail.account.degraded && (
            <div className="bench-note alert">
              本次抓取被判游客态，已<strong>保留上次抓到的作品列表</strong>（含作品 ID），没有被空数据覆盖。
            </div>
          )}

          {detail.fetch?.state === 'risk' && (
            <div className="bench-err">平台风控熔断：{detail.fetch.message || '已停止，请数小时后再试'}</div>
          )}
          {(detail.account.running || detail.fetch?.state === 'running') && detail.fetch?.message && (
            <div className="bench-note">{detail.fetch.message}</div>
          )}
          {/* 完成后也留一行汇总（含「还有 N 条未下载」这种要跟进的信息），而不是一闪而过 */}
          {!detail.account.running && detail.fetch?.state === 'done' && detail.fetch?.message && (
            <div className="bench-note dim">{detail.fetch.message}</div>
          )}
          {detail.account.last_error && detail.fetch?.state !== 'risk' && (
            <div className="bench-err">上次抓取失败：{detail.account.last_error}</div>
          )}

          <div className="bench-works-head">
            <span className="panel-title" style={{ margin: 0 }}>
              最新作品 {detail.works.length > 0 && `（${detail.works.length} 条）`}
            </span>
            {detail.works.length > 10 && (
              <button className="dash-more" onClick={() => setShowAllWorks((v) => !v)}>
                {showAllWorks ? '只看最新 10 条' : `显示全部 ${detail.works.length} 条`}
              </button>
            )}
          </div>

          {/* 表现监控概况：开了监控才出现，避免没开的时候白占一行 */}
          {(monStats.enabled > 0 || monStats.expired > 0 || sampling) && (
            <div className="bench-monitor-bar">
              <span className="bench-monitor-title">📈 表现监控</span>
              <span className="bench-monitor-meta">
                {monStats.enabled > 0 ? `监控中 ${monStats.enabled} 篇` : '暂无监控中的作品'}
                {monStats.expired > 0 ? ` · 已到期 ${monStats.expired} 篇` : ''}
                {monitorInfo ? ` · 每天 ${pad2(monitorInfo.hour)}:${pad2(monitorInfo.minute)} 采样` : ''}
                {monStats.enabled > 0 && monitorInfo?.next_run_ts
                  ? ` · 下次 ${fmtWhen(monitorInfo.next_run_ts)}` : ''}
              </span>
              {sampling?.running && (
                <span className="bench-monitor-run">
                  采样中 {sampling.done}/{sampling.total || '…'}
                </span>
              )}
              <button className="bench-act" onClick={sampleAllNow}
                disabled={!!sampling?.running || monStats.enabled === 0}
                title="马上把本账号监控中的作品各采一次（会覆盖今天的采样点）">
                ⟳ 立即采样
              </button>
            </div>
          )}

          {detail.works.length === 0 && (
            <div className="card bench-empty">
              {detail.account.running || detail.fetch?.state === 'running'
                ? '正在抓取…（首次约 30-60 秒）'
                : '还没有作品数据。点右上角「获取最新作品」抓一次。'}
            </div>
          )}

          <div className="wl-list">
            <div className="wl-head">
              <div className="wl-work">作品</div>
              <div className="wl-num">阅读量</div>
              <div className="wl-num">点赞</div>
              <div className="wl-num">收藏</div>
              <div className="wl-num">评论</div>
              <div className="wl-mon-cell">监控</div>
              <div className="wl-actions">操作</div>
            </div>

            {(showAllWorks ? detail.works : detail.works.slice(0, 10)).map((w, i, visible) => {
              const f = (detail.platform as BenchPlatform)?.fields || {};
              const key = w.id || `${i}-${w.title}`;
              const menuOpen = openMenu === key;
              const cover = w.cover_path ? mediaUrl(w.cover_path) : w.cover_url;
              // 更新时间 = 这条作品的表现数据最近一次采集时间（监控中的用采样时间，
              // 其余用账号最近抓取时间）；不是笔记本身的编辑时间（那只有下载过的才有）
              const updatedSec = w.monitor?.last_ts || detail.fetched_at || 0;
              return (
                <div key={key}
                  className={`wl-row${w.is_burst ? ' burst' : ''}${w.id ? ' clickable' : ''}`
                    + `${i === 0 ? ' first' : ''}`}
                  onClick={() => { setOpenMenu(null); if (w.id) setTrendWork(w); }}>
                  <div className="wl-work">
                    <div className="wl-cover-wrap">
                      {cover ? (
                        <img className="wl-cover" src={cover} alt="" loading="lazy"
                          referrerPolicy="no-referrer" />
                      ) : (
                        <div className="wl-cover ph">📝</div>
                      )}
                    </div>
                    <div className="wl-info">
                      <div className="wl-title" title={w.title}>
                        <span className="wl-title-text">{w.title}</span>
                        {w.is_burst && <span className="wl-tag burst">爆款</span>}
                        {w.is_top && <span className="wl-tag top">置顶</span>}
                      </div>
                      <div className="wl-time">发布时间 {fmtDateTime(w.published_at_ms)}</div>
                      <div className="wl-time">更新时间 {fmtDateTime(updatedSec * 1000)}</div>
                      {/* 原文链接单独一行（在「更新时间」下面）；点它不要顺带打开趋势弹窗 */}
                      {w.url && (
                        <a className="wl-link" href={w.url} target="_blank" rel="noreferrer"
                          title="在平台打开这条作品"
                          onClick={(e) => e.stopPropagation()}>
                          原文 ↗
                        </a>
                      )}
                    </div>
                  </div>

                  <div className={`wl-num${f.work_plays ? '' : ' wl-na'}`}
                    title={f.work_plays ? '阅读量（平台的播放/观看次数）'
                      : '该平台不公开阅读量，显示「—」，不估算'}>
                    {f.work_plays ? fmtNum(w.plays) : '—'}
                  </div>
                  <div className="wl-num">{fmtNum(w.likes)}</div>
                  <div className="wl-num">{fmtNum(w.favorites)}</div>
                  <div className="wl-num">{fmtNum(w.comments)}</div>

                  <div className="wl-mon-cell" onClick={(e) => e.stopPropagation()}>
                    {w.id && (
                      <button
                        className={`bench-mon${w.monitor?.enabled ? ' on' : ''}`
                          + `${w.monitor?.expired && !w.monitor?.enabled ? ' expired' : ''}`}
                        disabled={busyWork === w.id}
                        title={w.monitor?.enabled
                          ? `监控中：已记录 ${w.monitor.days}/${w.monitor.window_days} 天，`
                            + `每天 ${pad2(monitorInfo?.hour ?? 9)}:${pad2(monitorInfo?.minute ?? 0)} 自动采样；点一下停止`
                          : w.monitor?.expired
                            ? '这一轮已记满 15 天并自动停止；点一下重新开始'
                            : '开启监控：每天固定时间记录点赞/收藏/评论，最多连续 15 天'}
                        onClick={() => toggleMonitor(w)}>
                        <i className="bench-mon-dot" />
                        {w.monitor?.enabled
                          ? `监控中 ${w.monitor.days}天`
                          : w.monitor?.expired ? '已到期' : '监控'}
                      </button>
                    )}
                  </div>

                  <div className="wl-actions" onClick={(e) => e.stopPropagation()}>
                    {w.id && (
                      <button className="wl-act"
                        title={`查看监控期间${f.work_plays ? '点赞/收藏/评论/播放量' : '点赞/收藏/评论'}的变化趋势`
                          + '（有点的日子才画，最多 15 天）'}
                        onClick={() => { setOpenMenu(null); setTrendWork(w); }}>趋势</button>
                    )}
                    {detail.account.platform === 'xiaohongshu' && w.id && (
                      w.downloaded && w.download ? (
                        <a className="wl-dl" target="_blank" rel="noreferrer"
                          href={mediaUrl(w.download.note_md)}
                          title={`已下载 ${w.download.files} 个文件（${(w.download.bytes / 1048576).toFixed(1)}MB）`
                            + ` · 打开笔记记录（含正文/标签/互动数据）`}>
                          ✓ {w.download.files}
                        </a>
                      ) : (
                        <button className="wl-act" disabled={w.downloading || busyWork === w.id}
                          title="下载这条笔记的全部数据：图片原图（含动图）+ 视频（默认最低画质，省磁盘）+ 元数据记录"
                          onClick={() => downloadWork(w)}>
                          {w.downloading || busyWork === w.id ? '…' : '下载'}
                        </button>
                      )
                    )}
                    <button className={`wl-act${menuOpen ? ' on' : ''}`} title="更多操作"
                      aria-haspopup="menu" aria-expanded={menuOpen}
                      onClick={() => setOpenMenu(menuOpen ? null : key)}>⋯</button>

                    {menuOpen && (
                      // 最后两行朝上弹，否则菜单会被列表/视口下边缘截掉
                      <div className={`wl-menu${i >= visible.length - 2 ? ' up' : ''}`} role="menu">
                        {/* 「原文 ↗」已经挪到「更新时间」下面那一行，这里不重复 */}
                        {onBreakdown && (
                          <button className="wl-menu-item" onClick={() => {
                            setOpenMenu(null);
                            onBreakdown(
                              `${w.title}\n（来源：${detail.account.nickname || detail.account.account_id} · `
                              + `${detail.account.platform_name} · 点赞 ${w.likes ?? '—'}）`);
                          }}>拆解</button>
                        )}
                        <button className="wl-menu-item" onClick={() => {
                          setOpenMenu(null);
                          toIdeas(w, detail.account);
                        }}>存选题</button>
                        {w.detail_available && !w.detail_fetched && (
                          <button className="wl-menu-item" onClick={() => {
                            setOpenMenu(null);
                            deepDive(w);
                          }}>深挖</button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {openMenu && (
            <div className="wl-menu-backdrop" onClick={() => setOpenMenu(null)} />
          )}

          {detail.works.length > 0 && !(detail.platform as BenchPlatform)?.fields?.work_favorites
            && !(detail.platform as BenchPlatform)?.fields?.work_comments && (
            <p className="bench-foot">
              {(detail.platform as BenchPlatform)?.name} 的作品收藏/评论数不在公开主页里
              （需要作者后台，或逐条详情页——而该平台不返回作品 ID，因此无法深挖），所以显示「—」。
            </p>
          )}
        </>
      )}

      {selected && !detail && (
        <div className="loading" style={{ padding: 40 }}><div className="spinner" />加载中…</div>
      )}

      {trendWork && selected && (
        <WorkTrendModal
          bid={selected}
          workId={trendWork.id}
          workTitle={trendWork.title}
          onClose={() => setTrendWork(null)}
          onChanged={refreshMonitorViews}
        />
      )}

      {cookieForm && (
        <CookieModal initialPlatform={cookieForm.platform}
          onClose={() => setCookieForm(null)}
          onSaved={async () => {
            setCookieForm(null);
            setCookies(await fetchBenchCookies());
            showToast('cookie 已保存——之后该平台走纯 HTTP，不用浏览器');
          }} />
      )}

      {/* ---------------- 新增弹窗 ---------------- */}
      {form && (
        <div className="overlay" onClick={() => setForm(null)}>
          <div className="modal" style={{ width: 460, maxWidth: '100%' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={{ margin: 0 }}>添加对标账号</h3>
              <button className="icon-btn" onClick={() => setForm(null)}>×</button>
            </div>

            <label className="field-label">平台</label>
            <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap' }}>
              {platforms.map((p) => (
                <button key={p.platform} className={`chip ${form.platform === p.platform ? 'active' : ''}`}
                  onClick={() => setForm({ ...form, platform: p.platform })}>
                  {p.name}
                  {p.auto && p.experimental && <span className="chip-exp">未实测</span>}
                  {!p.auto && <span className="chip-exp dim">手动</span>}
                </button>
              ))}
            </div>

            <label className="field-label">
              {platMeta[form.platform]?.id_label || '账号 ID'}
            </label>
            <input className="field" value={form.target} autoFocus
              placeholder={platMeta[form.platform]?.id_hint || '主页链接或账号 ID'}
              onChange={(e) => setForm({ ...form, target: e.target.value })} />

            <label className="field-label">备注</label>
            <input className="field" value={form.remark} placeholder="如：同赛道头部，重点看选题"
              onChange={(e) => setForm({ ...form, remark: e.target.value })} />

            {platMeta[form.platform] && (
              <div className={`bench-hint ${platMeta[form.platform].auto ? '' : 'warn'}`}>
                {platMeta[form.platform].auto
                  ? <>可自动抓取：{platMeta[form.platform].note}</>
                  : <>暂不支持自动抓取，只能手动登记账号信息：{platMeta[form.platform].note}</>}
              </div>
            )}
            {manualPlatforms.length > 0 && (
              <div className="bench-hint dim">
                已支持自动抓取：{autoPlatforms.map((p) => p.name).join('、') || '（暂无）'}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
              <button className="btn btn-sm" onClick={() => setForm(null)}>取消</button>
              <button className="btn btn-sm btn-primary" onClick={submit}
                disabled={busy || !form.target.trim()}>{busy ? '添加中…' : '添加'}</button>
            </div>
          </div>
        </div>
      )}

      {toast && <div className="toast ok"><span className="toast-icon">✓</span>{toast}</div>}    </div>
  );
}
