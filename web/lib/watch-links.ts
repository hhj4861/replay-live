export type WatchLinkKind = 'channel' | 'broadcast';

const invalidWatchUrl = '선택한 플랫폼의 HTTPS 채널 또는 방송 링크를 확인하세요.';
const trackingQueries = new Set(['si', 'feature', 't', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'fbclid', 'igsh', 'mibextid']);
const hosts: Record<string, Record<string, string>> = {
  youtube: { 'youtube.com': 'www.youtube.com', 'www.youtube.com': 'www.youtube.com', 'm.youtube.com': 'www.youtube.com', 'youtu.be': 'youtu.be' },
  twitch: { 'twitch.tv': 'www.twitch.tv', 'www.twitch.tv': 'www.twitch.tv' },
  facebook: { 'facebook.com': 'www.facebook.com', 'www.facebook.com': 'www.facebook.com', 'm.facebook.com': 'www.facebook.com' },
  instagram: { 'instagram.com': 'www.instagram.com', 'www.instagram.com': 'www.instagram.com' },
  tiktok: { 'tiktok.com': 'www.tiktok.com', 'www.tiktok.com': 'www.tiktok.com' },
  naver: { 'tv.naver.com': 'tv.naver.com' }, chzzk: { 'chzzk.naver.com': 'chzzk.naver.com' },
  kick: { 'kick.com': 'kick.com', 'www.kick.com': 'kick.com' },
};
const reserved: Record<string, Set<string>> = Object.fromEntries(Object.entries({
  youtube: ['watch', 'live', 'shorts'],
  twitch: ['videos', 'directory', 'downloads', 'settings', 'login', 'signup', 'search', 'subscriptions', 'wallet', 'inventory', 'moderator', 'p'],
  facebook: ['watch', 'video.php', 'profile.php', 'login', 'groups', 'share', 'sharer.php', 'dialog'],
  instagram: ['accounts', 'p', 'reel', 'reels', 'explore', 'direct', 'stories', 'live'],
  tiktok: ['login', 'signup', 'explore'], naver: ['v', 'l'],
  kick: ['videos', 'video', 'categories', 'search', 'settings', 'login', 'register'],
}).map(([target, names]) => [target, new Set(names)]));

function publicIPv4(host: string): boolean {
  const pieces = host.split('.');
  if (pieces.length !== 4 || pieces.some(piece => !/^(0|[1-9][0-9]{0,2})$/.test(piece) || Number(piece) > 255)) return false;
  const [a, b, c, d] = pieces.map(Number);
  return !(a === 0 || a === 10 || a === 127 || a >= 224
    || (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && (b === 168 || (b === 0 && (c === 2 || (c === 0 && d !== 9 && d !== 10)))))
    || (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) || (a === 203 && b === 0 && c === 113));
}

function publicLookingHost(host: string): boolean {
  if (/^[0-9.]+$/.test(host)) return publicIPv4(host);
  // These links are displayed, never fetched here. A DNS-shaped name is not a
  // claim that its current DNS resolution is public; RTMP has a separate policy.
  return host.length <= 253 && /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(host)
    && !/\.(localhost|local|internal|lan|home)$/.test(host) && !host.split('.').some(label => label.startsWith('0x'));
}

function hasControl(value: string): boolean {
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code < 32 || (code >= 127 && code <= 159)) return true;
  }
  return false;
}

function decodePart(value: string, query = false): string {
  if (/%(?![a-fA-F0-9]{2})/.test(value)) throw new Error(invalidWatchUrl);
  const decoded = decodeURIComponent(query ? value.replace(/\+/g, ' ') : value);
  if (hasControl(decoded) || /[%\\#?/]/.test(decoded)) throw new Error(invalidWatchUrl);
  return decoded;
}

function encodePart(value: string): string {
  return encodeURIComponent(value).replace(/[!'()*]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
}

/** Normalize a user supplied viewing link without fetching it or inferring any
 * live/archive status. Only known tracking parameters are stripped; unknown
 * parameters (including credentials/tokens and redirect URLs) are rejected. */
export function normalizeWatchUrl(target: string, value: string, kind: WatchLinkKind = 'channel'): string {
  try {
    if (typeof value !== 'string' || value.length > 2048 || hasControl(value) || value.includes('\\')
        || !['channel', 'broadcast'].includes(kind) || !['local', 'custom', ...Object.keys(hosts)].includes(target)) throw new Error();
    const input = value.trim();
    if (!input) return '';
    if (target === 'local' || input.includes('#')) throw new Error();
    const raw = /^https:\/\/([A-Za-z0-9.-]+)(?::443)?(\/[^?#]*)?(?:\?([^#]*))?$/i.exec(input);
    if (!raw) throw new Error();
    let host = raw[1].toLowerCase();
    if (target === 'custom') { if (!publicLookingHost(host)) throw new Error(); }
    else { if (!Object.hasOwn(hosts[target], host)) throw new Error(); host = hosts[target][host]; }
    const path = (raw[2] || '/').replace(/\/+$/, '') || '/';
    let parts = path === '/' ? [] : path.slice(1).split('/').map(part => {
      const decoded = decodePart(part);
      if ((!decoded && target !== 'custom') || decoded === '.' || decoded === '..') throw new Error();
      return decoded;
    });
    const query = new Map<string, string>();
    const seen = new Set<string>();
    if (raw[3] && raw[3].split('&').length > 20) throw new Error();
    if (raw[3]) for (const entry of raw[3].split('&')) {
      const split = entry.indexOf('=');
      const name = decodePart(split < 0 ? entry : entry.slice(0, split), true);
      const val = decodePart(split < 0 ? '' : entry.slice(split + 1), true);
      if (!name || seen.has(name)) throw new Error();
      seen.add(name);
      if (trackingQueries.has(name)) continue;
      if (!['v', 'id'].includes(name)) throw new Error();
      query.set(name, val);
    }
    const noQuery = () => query.size === 0;
    const singleQuery = (key: string, pattern: RegExp) => query.size === 1 && pattern.test(query.get(key) || '');
    const slug = (name: string, pattern: RegExp) => pattern.test(name) && !reserved[target]?.has(name.toLowerCase());
    const id = '[A-Za-z0-9_-]{11}';
    const videoId = new RegExp(`^${id}$`);
    const hex = /^[a-fA-F0-9]{32}$/;
    const uuid = /^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$/;
    let valid = false;
    if (target === 'custom') valid = [...query.values()].every(item => /^[A-Za-z0-9._-]+$/.test(item));
    else if (target === 'youtube') {
      if (kind === 'channel') {
        if (parts.at(-1) === 'live') parts = parts.slice(0, -1);
        valid = host !== 'youtu.be' && noQuery() && ((parts.length === 1 && /^@[\p{L}\p{N}._-]+$/u.test(parts[0]))
          || (parts.length === 2 && ['channel', 'c', 'user'].includes(parts[0])
            && slug(parts[1], parts[0] === 'channel' ? /^[A-Za-z0-9_-]+$/ : /^[A-Za-z0-9._-]+$/)));
      } else if (host === 'youtu.be') {
        valid = parts.length === 1 && videoId.test(parts[0]) && noQuery();
        if (valid) { host = 'www.youtube.com'; query.set('v', parts[0]); parts = ['watch']; }
      } else valid = (parts.length === 1 && parts[0] === 'watch' && singleQuery('v', videoId))
        || (parts.length === 2 && ['live', 'shorts'].includes(parts[0]) && videoId.test(parts[1]) && noQuery());
    } else if (target === 'twitch') valid = noQuery() && (kind === 'channel'
      ? parts.length === 1 && slug(parts[0], /^[A-Za-z0-9_]{1,25}$/)
      : parts.length === 2 && parts[0] === 'videos' && /^\d+$/.test(parts[1]));
    else if (target === 'facebook') valid = kind === 'channel'
      ? (parts.length === 1 && slug(parts[0], /^[A-Za-z0-9._-]+$/) && noQuery())
        || (parts.length === 1 && parts[0] === 'profile.php' && singleQuery('id', /^\d+$/))
      : (parts.length === 3 && slug(parts[0], /^[A-Za-z0-9._-]+$/) && parts[1] === 'videos' && /^\d+$/.test(parts[2]) && noQuery())
        || (parts.length === 1 && ['watch', 'video.php'].includes(parts[0]) && singleQuery('v', /^\d+$/));
    else if (target === 'instagram') valid = noQuery() && (kind === 'channel'
      ? parts.length === 1 && slug(parts[0], /^[A-Za-z0-9._]+$/)
      : (parts.length === 2 && slug(parts[0], /^[A-Za-z0-9._]+$/) && parts[1] === 'live')
        || (parts.length === 2 && ['p', 'reel'].includes(parts[0]) && /^[A-Za-z0-9_-]+$/.test(parts[1])));
    else if (target === 'tiktok') valid = noQuery() && /^@[A-Za-z0-9._]+$/.test(parts[0] || '')
      && slug(parts[0].slice(1), /^[A-Za-z0-9._]+$/) && (kind === 'channel' ? parts.length === 1
        : (parts.length === 2 && parts[1] === 'live') || (parts.length === 3 && parts[1] === 'video' && /^\d+$/.test(parts[2])));
    else if (target === 'naver') valid = noQuery() && (kind === 'channel'
      ? parts.length === 1 && slug(parts[0], /^[A-Za-z0-9._-]+$/)
      : parts.length === 2 && ['v', 'l'].includes(parts[0]) && /^\d+$/.test(parts[1]));
    else if (target === 'chzzk') {
      valid = noQuery() && (kind === 'channel' ? parts.length === 1 && hex.test(parts[0])
        : parts.length === 2 && ((parts[0] === 'live' && hex.test(parts[1])) || (parts[0] === 'video' && /^\d+$/.test(parts[1]))));
      if (valid) parts = parts.map(part => hex.test(part) ? part.toLowerCase() : part);
    } else if (target === 'kick') valid = noQuery() && (kind === 'channel'
      ? parts.length === 1 && slug(parts[0], /^[A-Za-z0-9_-]+$/)
      : (parts.length === 3 && slug(parts[0], /^[A-Za-z0-9_-]+$/) && parts[1] === 'videos' && uuid.test(parts[2]))
        || (parts.length === 2 && parts[0] === 'video' && uuid.test(parts[1])));
    if (!valid) throw new Error();
    const search = [...query].sort(([a], [b]) => a.localeCompare(b)).map(([name, val]) => `${name}=${encodePart(val)}`).join('&');
    const normalized = `https://${host}/${parts.map(part => encodePart(part).replace(/%40/g, '@')).join('/')}${search ? `?${search}` : ''}`;
    if (normalized.length > 2048) throw new Error();
    return normalized;
  } catch { throw new Error(invalidWatchUrl); }
}

export function watchUrlError(target: string, value: string, kind: WatchLinkKind = 'channel'): string | null {
  try { normalizeWatchUrl(target, value, kind); return null; } catch { return invalidWatchUrl; }
}
