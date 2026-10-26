/** 趋势相关的展示格式化（作品监控用）。
 *
 *  单独放一个文件而不是塞进组件：三个地方要用同一套口径——作品卡/页头（`fmtWhen`）、
 *  趋势弹窗标题（`fmtValue`/`fmtDate`）、折线图坐标轴（`fmtValue`/`fmtDay`）。
 *  数字口径与作品卡、账号表保持一致（中文缩写，不出现 96300000）。
 */
export const pad2 = (n: number): string => String(n).padStart(2, '0');

/** 指标展示：1.2万 / 1.3亿 / 1,234。 */
export function fmtValue(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (Math.abs(n) >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
  if (Math.abs(n) >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return n.toLocaleString('zh-CN');
}

/** 「2026-10-09」→「10-09」。 */
export function fmtDay(day: string): string {
  const m = /^\d{4}-(\d{2})-(\d{2})$/.exec(day || '');
  return m ? `${m[1]}-${m[2]}` : day || '';
}

/** 秒级时间戳 → 「今天 09:00」/「明天 09:00」/「10月19日 09:00」。 */
export function fmtWhen(ts: number | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  const key = (x: Date): string => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  const now = new Date();
  if (key(d) === key(now)) return `今天 ${hm}`;
  if (key(d) === key(new Date(now.getTime() + 86400000))) return `明天 ${hm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

/** 秒级时间戳 → 「10月9日」。 */
export function fmtDate(ts: number | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

/** 毫秒时间戳 → 「今天 12:05」/「10-16 20:14」/「2025-09-24 21:04」（发布时间、更新时间用）。 */
export function fmtDateTime(ms: number | null | undefined): string {
  if (!ms) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return `今天 ${hm}`;
  if (d.getFullYear() === now.getFullYear()) {
    return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}`;
  }
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}`;
}
