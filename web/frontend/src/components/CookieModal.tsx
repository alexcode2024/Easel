/**
 * 平台 Cookie 弹窗（共用组件）
 *
 * 为什么要有它：小红书的读数据/校验现在走**纯 HTTP**（curl_cffi 伪装 TLS 指纹），
 * 凭证就是用户从自己浏览器里复制的 cookie —— 不需要扫码、不需要浏览器窗口，
 * 也就不会撞上扫码登录页的 300012。
 *
 * 注意小红书是**两套独立会话**：
 *   - 主站 www.xiaohongshu.com（读数据）→ web_session
 *   - 创作平台 creator.xiaohongshu.com（发布/后台）→ galaxy_creator_session_id 等
 * 界面上要说清这一点，否则用户只粘一半会导致另一半不可用。
 */
import { useEffect, useState } from 'react';
import {
  clearBenchCookie, fetchBenchCookies, saveBenchCookie,
  type BenchCookieStatus,
} from '../lib/api';

function timeAgo(ts?: number): string {
  if (!ts) return '';
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

export default function CookieModal(
  { onClose, onSaved, initialPlatform = 'xiaohongshu' }:
  { onClose: () => void; onSaved?: (platform: string) => void; initialPlatform?: string },
) {
  const [cookies, setCookies] = useState<BenchCookieStatus[]>([]);
  const [platform, setPlatform] = useState(initialPlatform);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const reload = () => fetchBenchCookies().then(setCookies).catch(() => {});
  useEffect(() => { reload(); }, []);

  const save = async () => {
    setBusy(true); setErr('');
    try {
      await saveBenchCookie(platform, value);
      setValue('');
      await reload();
      onSaved?.(platform);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'cookie 保存失败');
    } finally { setBusy(false); }
  };

  const drop = async (p: string, name: string) => {
    if (!window.confirm(`清除 ${name} 的 cookie？清除后该平台会退回浏览器取数。`)) return;
    try {
      await clearBenchCookie(p);
      await reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : '清除失败');
    }
  };

  const cur = cookies.find((c) => c.platform === platform);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" style={{ width: 580, maxWidth: '100%' }} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0 }}>平台 Cookie</h3>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>

        <p className="cookie-lead">
          在<strong>你自己已登录</strong>的浏览器里复制 cookie 贴到下面。小红书有了它就能
          <strong>走纯 HTTP 取数</strong>——不起浏览器、不弹窗、也不会被判 300012，
          还能拿到真实粉丝数与作品 ID。
        </p>

        <label className="field-label">平台</label>
        <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap' }}>
          {cookies.map((c) => (
            <button key={c.platform}
              className={`chip ${platform === c.platform ? 'active' : ''}`}
              onClick={() => { setPlatform(c.platform); setValue(''); setErr(''); }}>
              {c.name}{c.saved && <span className="chip-exp ok">已配置</span>}
            </button>
          ))}
        </div>

        {cur?.saved && (
          <div className="cookie-state">
            <div>
              <strong>已保存</strong> {cur.cookie_count} 条 cookie
              {cur.has_required
                ? `（含必需的 ${cur.required.join('、')}）`
                : `（缺少 ${cur.missing?.join('、')}）`}
              {cur.saved_at ? ` · ${timeAgo(cur.saved_at)}` : ''}
            </div>
            <button className="bench-act" onClick={() => drop(cur.platform, cur.name)}>清除</button>
          </div>
        )}

        <label className="field-label">
          粘贴 Cookie 值
          {cur?.required.length
            ? <span className="bench-muted"> （必须包含 {cur.required.join('、')}）</span>
            : null}
        </label>
        <textarea className="field" style={{ minHeight: 92, fontSize: 12 }}
          value={value}
          placeholder="web_session=xxxx; a1=yyyy; webId=zzzz; …（整串粘贴即可）"
          onChange={(e) => setValue(e.target.value)} />

        {platform === 'xiaohongshu' && (
          <div className="cookie-note">
            <strong>小红书是两套独立会话，按用途分别复制：</strong>
            <ul>
              <li><b>读数据</b>（对标账号）：在 <code>www.xiaohongshu.com</code> 登录后复制，
                关键项 <code>web_session</code> —— <b>只需要这个就能用</b></li>
              <li><b>自动发布</b>（可选）：要让程序替你发笔记时，再在
                <code>creator.xiaohongshu.com</code> 登录后复制，
                关键项 <code>galaxy_creator_session_id</code>、<code>customer-sso-sid</code>；
                <b>手动发布不需要它</b></li>
            </ul>
            一次粘贴整串也可以——把两个站点的 cookie 都带上即可（程序按需取用）。
          </div>
        )}

        <div className="bench-hint">
          <strong>怎么拿：</strong>用 Chrome/Edge 打开并登录对应站点 → F12 → Network → 刷新页面 →
          点任一请求 → Request Headers 里找到 <code>cookie:</code> → 整行值复制过来。<br />
          也可以用 Application → Cookies → 全选复制。
        </div>

        {err && <div className="bench-err" style={{ marginTop: 8 }}>{err}</div>}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <button className="btn btn-sm" onClick={onClose}>取消</button>
          <button className="btn btn-sm btn-primary" onClick={save}
            disabled={busy || !value.trim()}>{busy ? '保存中…' : '保存'}</button>
        </div>
      </div>
    </div>
  );
}
