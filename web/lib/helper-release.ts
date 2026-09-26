export type HelperRelease = { version: string; development?: boolean; downloads: { label: string; url: string; sha256: string }[] };
const prefix = 'https://github.com/hhj4861/replay-live/releases/download/';
export async function helperRelease(signal: AbortSignal): Promise<HelperRelease | null> {
  const response = await fetch('/helper-release.json', { signal, credentials: 'omit', cache: 'no-store' });
  if (!response.ok) throw new Error('설치 파일 안내를 불러오지 못했습니다. 다시 확인해 주세요.');
  const value = await response.json() as HelperRelease;
  if (value?.version === null) return null;
  if (!value || typeof value.version !== 'string' || !/^\d+\.\d+\.\d+$/.test(value.version) || !Array.isArray(value.downloads) || !value.downloads.length) throw new Error('설치 파일 정보를 확인할 수 없습니다.');
  if (value.development !== undefined && typeof value.development !== 'boolean') throw new Error('설치 파일 정보를 확인할 수 없습니다.');
  const allowed = new Set(['macOS Apple Silicon', 'macOS Intel', 'Windows x64']);
  for (const item of value.downloads) {
    if (!allowed.has(item.label) || typeof item.url !== 'string' || !item.url.startsWith(`${prefix}helper-v${value.version}/`)
      || !/^ReplayLiveHelper-[A-Za-z0-9._-]+\.zip$/.test(item.url.slice(item.url.lastIndexOf('/') + 1))
      || !/^[a-f0-9]{64}$/.test(item.sha256)) throw new Error('설치 파일 정보를 확인할 수 없습니다.');
  }
  return value;
}
