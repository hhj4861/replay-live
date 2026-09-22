import type { HelperRelease } from './helper-release';

type Hints = { platform?: string; architecture?: string; bitness?: string };
type DeviceNavigator = {
  userAgent: string; platform?: string; maxTouchPoints?: number;
  userAgentData?: { platform?: string; mobile?: boolean; getHighEntropyValues?: (keys: string[]) => Promise<Hints> };
};
export type HelperPlatform = {
  os: 'macos' | 'windows' | 'unsupported' | 'unknown'; label: string | null; message: string;
};

// Only OS/CPU information is used locally; no model, device ID or server request.
export async function detectHelperPlatform(nav: DeviceNavigator = navigator): Promise<HelperPlatform> {
  const data = nav.userAgentData;
  const platform = data?.platform || nav.platform || '';
  if (data?.mobile || /Android|iPhone|iPad|iPod/i.test(nav.userAgent)
      || (/Mac/i.test(platform) && (nav.maxTouchPoints || 0) > 1)) {
    return { os: 'unsupported', label: null, message: '모바일에서는 도우미를 설치할 수 없습니다. Windows 또는 Mac에서 이용해 주세요.' };
  }
  const os = /Mac/i.test(platform) || /Macintosh/i.test(nav.userAgent) ? 'macos'
    : /Win/i.test(platform) || /Windows/i.test(nav.userAgent) ? 'windows'
      : /Linux|CrOS/i.test(platform + nav.userAgent) ? 'unsupported' : 'unknown';
  if (os === 'unsupported') return { os, label: null, message: '현재 PC용 도우미는 제공하지 않습니다. MP4 파일 업로드를 이용해 주세요.' };
  let hints: Hints = {};
  if (data?.getHighEntropyValues) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      hints = await Promise.race([
        data.getHighEntropyValues(['architecture', 'bitness']),
        new Promise<Hints>(resolve => { timer = setTimeout(() => resolve({}), 1500); }),
      ]);
    } catch { /* Browser policy may withhold hardware hints. */ }
    finally { clearTimeout(timer); }
  }
  const arch = hints.architecture?.toLowerCase();
  if (os === 'macos' && hints.bitness === '64' && (arch === 'arm' || arch === 'x86')) {
    const label = arch === 'arm' ? 'macOS Apple Silicon' : 'macOS Intel';
    return { os, label, message: `${label} PC를 확인했습니다. 설치 파일을 자동으로 선택합니다.` };
  }
  if (os === 'windows' && arch === 'x86' && hints.bitness === '64') {
    return { os, label: 'Windows x64', message: 'Windows x64 PC를 확인했습니다. 설치 파일을 자동으로 선택합니다.' };
  }
  if (os === 'windows' && (arch === 'arm' || hints.bitness === '32')) {
    return { os: 'unsupported', label: null, message: '현재 도우미는 Windows x64용입니다. 이 PC에서는 MP4 파일 업로드를 이용해 주세요.' };
  }
  // macOS User-Agent often says Intel even on Apple Silicon. Never guess.
  return { os, label: null, message: '브라우저가 CPU 정보를 제공하지 않아 자동 선택하지 못했습니다. PC 종류를 한 번 선택해 주세요.' };
}

export function platformDownloads(release: HelperRelease | null, platform: HelperPlatform | null) {
  if (!release || !platform || platform.os === 'unsupported') return [];
  return release.downloads.filter(item => platform.os === 'unknown'
    || (platform.os === 'macos' ? item.label.startsWith('macOS ') : item.label === 'Windows x64'));
}

// Must be called directly from the user's confirmation click. A click is not
// proof of download or installation; keep the retry and OS approval guidance.
export function requestHelperDownload(item: HelperRelease['downloads'][number]): boolean {
  try {
    const anchor = document.createElement('a');
    anchor.href = item.url; anchor.download = ''; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
    anchor.hidden = true; document.body.appendChild(anchor);
    try { anchor.click(); } finally { anchor.remove(); }
    return true;
  } catch { return false; }
}
