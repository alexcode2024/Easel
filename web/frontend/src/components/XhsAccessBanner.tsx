import type { XhsAccessStatus } from '../lib/api';

export default function XhsAccessBanner({ status }: { status: XhsAccessStatus | null }) {
  if (!status) return null;
  return (
    <div className={`xhs-access-bar${status.risk.active ? ' risk' : ''}`} role="status">
      <span className="xhs-access-dot" />
      {status.risk.active
        ? `小红书访问已暂停：${status.risk.reason || '命中风控'}（最早 ${new Date(status.risk.until * 1000).toLocaleString('zh-CN')} 后可提交新任务）`
        : status.active
          ? `正在访问小红书：${status.active.label} · 同一登录态并发 1 · 排队 ${status.queued} 个`
          : `小红书访问${status.queued ? '排队中' : '空闲'} · 同一登录态并发 1`}
      {status.risk.active && <span> · 本轮已停止，不会自动重试</span>}
      {!status.risk.active && status.queue.map((entry) => (
        <div key={entry.id}>排队第 {entry.position} 位：{entry.label}</div>
      ))}
    </div>
  );
}
