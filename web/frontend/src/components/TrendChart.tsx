import type { BenchTrendRow } from '../lib/api';
import { fmtDay, fmtValue } from '../lib/trendFormat';

interface TrendChartProps {
  rows: BenchTrendRow[];
  color: string;
  /** 今天（「YYYY-MM-DD」）：右端只有确实是今天时才标「（今天）」。 */
  today?: string;
}

type Pt = { i: number; v: number; day: string };

/** 「好看」的刻度间距（1 / 2 / 2.5 / 5 × 10^k）：y 轴上是 2,000 而不是 2,903。 */
function niceStep(rough: number): number {
  if (!(rough > 0) || !Number.isFinite(rough)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  const mult = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return mult * mag;
}

/** 把取值区间向外扩到刻度整数倍，并给出 3–5 条网格线。 */
function niceScale(lo: number, hi: number): { lo: number; hi: number; ticks: number[] } {
  const span = hi - lo;
  if (!(span > 0)) {
    const bump = Math.max(1, Math.abs(hi) * 0.1);
    return niceScale(hi - bump, hi + bump);
  }
  const step = niceStep(span / 3);
  const nLo = Math.floor(lo / step) * step;
  const nHi = Math.ceil(hi / step) * step;
  let ticks: number[] = [];
  for (let v = nLo; v <= nHi + step / 2; v += step) ticks.push(v);
  if (ticks.length > 5) ticks = ticks.filter((_, k) => k % 2 === 0);
  if (ticks.length < 2) ticks = [nLo, nHi];
  return { lo: nLo, hi: nHi, ticks };
}

/** 一条按自然日对齐的折线（纯 SVG，不引图表库）。
 *
 *  缺采样的日子不会补数据点；相邻真实观测值之间仍连线，跨过缺失日的那一段用虚线，
 *  既能读出变化方向，也不会把空白伪装成真实采样。
 */
export default function TrendChart({ rows, color, today }: TrendChartProps) {
  const W = 660;
  const H = 158;
  const pad = { left: 52, right: 16, top: 16, bottom: 26 };
  const iw = W - pad.left - pad.right;
  const ih = H - pad.top - pad.bottom;
  const n = rows.length;

  // 按日期顺序取真实观测值（没采到的日子没有点，不补 0、不重复前一天）
  const points: Pt[] = [];
  rows.forEach((r, i) => {
    if (r.value === null || r.value === undefined) return;
    points.push({ i, v: r.value, day: r.day });
  });

  // 相邻两个真实点之间直接连线；中间隔了没采样的日子就画虚线，表示「这段是跨越的，不是逐日实测」
  const edges = points.slice(1).map((b, k) => {
    const a = points[k];
    return { a, b, gap: b.i - a.i > 1 };
  });

  // 只有一个刻度时也贴左边：刚开始监控的那天就该出现在最左侧，而不是缩在中间/右边
  const x = (i: number): number => pad.left + (n <= 1 ? 0 : (iw * i) / (n - 1));

  let lo = 0;
  let hi = 1;
  let ticks: number[] = [];
  if (points.length > 0) {
    const values = points.map((p) => p.v);
    const minV = Math.min(...values);
    const maxV = Math.max(...values);
    // 上下各留一点余量再扩到整数刻度：线不贴边框，标签也是整千整百
    const padY = (maxV - minV) * 0.12 || Math.max(1, Math.abs(maxV) * 0.1);
    // 互动数是计数，不会是负数：下界钉在 0，别让 y 轴出现 -0.5 这种不可能的值
    const rawLo = minV >= 0 ? Math.max(0, minV - padY) : minV - padY;
    const scale = niceScale(rawLo, maxV + padY);
    lo = scale.lo;
    hi = scale.hi;
    ticks = scale.ticks;
  }
  const y = (v: number): number => pad.top + ih * (1 - (v - lo) / (hi - lo));

  const sampled = rows.filter((r) => r.sampled).length;
  const midIdx = Math.floor((n - 1) / 2);
  // 监控停掉/到期后，右端是最后一个采样日而不是今天，别硬标「今天」
  const lastIsToday = n > 0 && !!today && rows[n - 1].day === today;

  return (
    <div className="trend-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`这 ${n} 天的折线图，其中 ${sampled} 天有采样`}>
        {ticks.map((t, k) => (
          <g key={k}>
            <line x1={pad.left} x2={W - pad.right} y1={y(t)} y2={y(t)}
              stroke="var(--border)" strokeDasharray={k === 0 ? undefined : '3 4'}
              opacity={k === 0 ? 0.9 : 0.5} />
            <text x={pad.left - 9} y={y(t) + 3.5} textAnchor="end"
              className="trend-axis" fill="var(--text-tertiary)">{fmtValue(t)}</text>
          </g>
        ))}

        {/* 先画线，再画点：点压在线上，读数是实测值 */}
        {edges.map((e) => (
          <line key={`${e.a.day}-${e.b.day}`}
            x1={x(e.a.i).toFixed(1)} y1={y(e.a.v).toFixed(1)}
            x2={x(e.b.i).toFixed(1)} y2={y(e.b.v).toFixed(1)}
            stroke={color} strokeWidth={2}
            strokeLinecap="round"
            strokeDasharray={e.gap ? '5 4' : undefined}
            opacity={e.gap ? 0.75 : 1}>
            <title>
              {e.gap
                ? `${e.a.day} → ${e.b.day}（中间有没采到的日子，虚线是跨越连接）`
                : `${e.a.day} → ${e.b.day}`}
            </title>
          </line>
        ))}
        {points.map((p) => (
          <circle key={p.day} cx={x(p.i)} cy={y(p.v)} r={2.8} fill={color}
            stroke="var(--surface)" strokeWidth={1}>
            <title>{`${p.day} · ${fmtValue(p.v)}`}</title>
          </circle>
        ))}

        {points.length === 0 && (
          <text x={pad.left + iw / 2} y={pad.top + ih / 2} textAnchor="middle"
            className="trend-axis" fill="var(--text-tertiary)">
            这 {n} 天还没有采样数据
          </text>
        )}

        {n > 0 && (
          <>
            {/* 单日时左右两个标签会重复同一日期，只保留一个（贴在最左侧那个点上） */}
            {n > 1 && (
              <text x={pad.left} y={H - 8} textAnchor="start" className="trend-axis"
                fill="var(--text-tertiary)">{fmtDay(rows[0].day)}</text>
            )}
            {n > 2 && (
              <text x={x(midIdx)} y={H - 8} textAnchor="middle" className="trend-axis"
                fill="var(--text-tertiary)">{fmtDay(rows[midIdx].day)}</text>
            )}
            <text x={n > 1 ? W - pad.right : pad.left} y={H - 8}
              textAnchor={n > 1 ? 'end' : 'start'} className="trend-axis"
              fill="var(--text-tertiary)">
              {fmtDay(rows[n - 1].day)}{lastIsToday ? '（今天）' : ''}
            </text>
          </>
        )}
      </svg>
    </div>
  );
}
