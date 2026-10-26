import type { PlatformAccessStatus } from '../lib/api';

export default function PlatformAccessBanner({
  statuses,
}: {
  statuses: Record<string, PlatformAccessStatus> | null;
}) {
  if (!statuses) return null;
  const visible = Object.values(statuses).filter((status) =>
    status.risk.active || status.active || status.queued || status.external_busy,
  );
  if (!visible.length) return null;
  return (
    <div className="platform-access-list" role="status">
      {visible.map((status) => (
        <div className={`platform-access-bar${status.risk.active ? ' risk' : ''}`} key={status.platform}>
          <span className="platform-access-dot" />
          {status.risk.active
            ? `${status.platform_name}访问已暂停：${status.risk.reason || '命中风控'}（最早 ${new Date(status.risk.until * 1000).toLocaleString('zh-CN')} 后可提交新任务）`
            : status.active
              ? `正在访问${status.platform_name}：${status.active.label} · 并发 ${status.concurrency} · 排队 ${status.queued} 个`
              : status.external_busy
                ? `${status.platform_name}访问中（外部 CLI / 进程） · 并发 ${status.concurrency}`
                : `${status.platform_name}访问排队中 · 并发 ${status.concurrency} · 排队 ${status.queued} 个`}
          {status.risk.active && <span> · 本轮已停止，不会自动重试</span>}
          {!status.risk.active && status.queue.map((entry) => (
            <div key={entry.id}>排队第 {entry.position} 位：{entry.label}</div>
          ))}
        </div>
      ))}
    </div>
  );
}
