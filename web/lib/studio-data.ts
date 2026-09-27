/** Session-scoped configuration caching; user inventory is never cached here. */
export function createStudioConfigCache<T>(now = Date.now) {
  let generation = 0;
  let cached: { identity: string; expires: number; value: T } | undefined;
  return {
    clear() { generation += 1; cached = undefined; },
    async read(identity: string, load: () => Promise<T>): Promise<T> {
      if (cached?.identity === identity && now() < cached.expires) return cached.value;
      const version = ++generation;
      const value = await load();
      if (version === generation) cached = { identity, value, expires: now() + 60_000 };
      return value;
    },
  };
}

export function storageBreakdown(usage: { storage_bytes: number; storage_reserved_bytes: number }) {
  const reserved = Math.max(0, Math.min(usage.storage_bytes, usage.storage_reserved_bytes));
  return { stored: Math.max(0, usage.storage_bytes - reserved), reserved };
}

export function sourceDurationFailure(seconds?: number) {
  if (!seconds || !Number.isFinite(seconds) || seconds <= 0) return '영상이 허용 재생 시간을 초과합니다. 더 짧은 녹화 영상을 선택하세요.';
  return `가져올 수 있는 영상은 최대 ${Math.floor(seconds / 60)}분 ${Math.floor(seconds % 60)}초입니다. 이보다 짧은 영상을 선택하세요.`;
}
