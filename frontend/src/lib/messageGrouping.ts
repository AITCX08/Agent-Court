import type { Exchange } from './api';

export type DateBucket = 'today' | 'yesterday' | 'this_week' | 'earlier';

export const BUCKET_ORDER: DateBucket[] = ['today', 'yesterday', 'this_week', 'earlier'];

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

export function bucketOf(ts: string, now: Date): DateBucket {
  const t = new Date(ts);
  if (isNaN(t.getTime())) return 'earlier';
  const today0 = startOfDay(now).getTime();
  const day = 86400_000;
  const t0 = startOfDay(t).getTime();
  if (t0 === today0) return 'today';
  if (t0 === today0 - day) return 'yesterday';
  // 本周 = 最近 7 天内(不含今天/昨天)
  if (t0 > today0 - 7 * day) return 'this_week';
  return 'earlier';
}

export type DateGroup = {
  bucket: DateBucket;
  items: Exchange[];
};

/** 输入已按代表时间降序的 exchanges, 输出按 BUCKET_ORDER 排列的非空分组(组内保持降序)。 */
export function groupByDate(exchanges: Exchange[], now: Date): DateGroup[] {
  const map = new Map<DateBucket, Exchange[]>();
  for (const e of exchanges) {
    const b = bucketOf(e.timestamp, now);
    const arr = map.get(b) ?? [];
    arr.push(e);
    map.set(b, arr);
  }
  return BUCKET_ORDER
    .filter((b) => (map.get(b)?.length ?? 0) > 0)
    .map((b) => ({ bucket: b, items: map.get(b)! }));
}

/** 搜索: 匹配 user 或 assistant 正文(大小写不敏感)。空 query 全匹配。 */
export function matchSearch(e: Exchange, q: string): boolean {
  const query = q.trim().toLowerCase();
  if (!query) return true;
  const u = e.user?.content.toLowerCase() ?? '';
  const a = e.assistant?.content.toLowerCase() ?? '';
  return u.includes(query) || a.includes(query);
}
