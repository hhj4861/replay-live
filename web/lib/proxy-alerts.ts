type ProxyAlert = { id: string; lease_token: string; remaining_bytes: number; threshold_bytes: number };
type Control = (path: string, body?: unknown) => Promise<unknown>;
type Environment = Record<string, string | undefined>;
const purchaseUrl = 'https://app.dataimpulse.com/plans';

// Official User API, authenticated by this plan's proxy credentials. Do not use
// the reseller API's monetary deposit balance as remaining traffic.
export async function readProxyBalance(env: Environment, request: typeof fetch = fetch): Promise<{ remaining_bytes: number; observed_at: number }> {
  try {
    const proxy = new URL(env.REPLAY_SOURCE_PROXY_URL || '');
    if (proxy.protocol !== 'http:' || proxy.hostname !== 'gw.dataimpulse.com' || proxy.port !== '823'
        || proxy.pathname !== '/' || proxy.search || proxy.hash) throw new Error();
    const login = decodeURIComponent(proxy.username), password = decodeURIComponent(proxy.password);
    const hasControl = (value: string) => {
      for (let i = 0; i < value.length; i++) if (value.charCodeAt(i) < 32 || value.charCodeAt(i) === 127) return true;
      return false;
    };
    if (!login || !password || /[:\s]/.test(login) || hasControl(login) || hasControl(password)) throw new Error();
    const observed_at = Date.now() / 1000;
    const response = await request('https://gw.dataimpulse.com:777/api/stats', {
      method: 'GET', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(5000),
      headers: { Authorization: `Basic ${Buffer.from(`${login}:${password}`).toString('base64')}` },
    });
    if (!response.ok || !response.body) throw new Error();
    const reader = response.body.getReader();
    let length = 0;
    const chunks: Uint8Array[] = [];
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > 16_384) throw new Error();
        chunks.push(value);
      }
    } finally { await reader.cancel().catch(() => undefined); }
    const data = JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>;
    const values = [data.total_traffic, data.traffic_used, data.traffic_left];
    if (data.status !== 'ok' || data.login !== login
        || !values.every(value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 1e15)
        || (data.traffic_left as number) > (data.total_traffic as number)) throw new Error();
    return { remaining_bytes: data.traffic_left as number, observed_at };
  } catch { throw new Error('Proxy balance unavailable'); }
}

export async function monitorProxyQuota(control: Control, env: Environment, request: typeof fetch = fetch): Promise<boolean> {
  // A failed observation must never be treated as zero or cause a stale notice.
  const balance = await readProxyBalance(env, request);
  await control('proxy-balance', balance);
  return deliverProxyAlert(control, env, request);
}

export function proxyAlertText(alert: ProxyAlert): string {
  if (!Number.isSafeInteger(alert.remaining_bytes) || alert.remaining_bytes < 0
      || ![250_000_000, 1_000_000_000].includes(alert.threshold_bytes)) throw new Error('Invalid proxy alert');
  const remaining = alert.remaining_bytes >= 1_000_000_000
    ? `${(alert.remaining_bytes / 1_000_000_000).toFixed(2)} GB`
    : `${Math.floor(alert.remaining_bytes / 1_000_000)} MB`;
  return `Replay Live · ${alert.threshold_bytes === 250_000_000 ? '프록시 용량 긴급 알림' : '프록시 용량 알림'}\n`
    + `남은 용량: ${remaining}\n모든 사용자의 링크 가져오기가 함께 사용하는 용량입니다.\n`
    + `충전 확인: ${purchaseUrl}\n자동 결제는 하지 않습니다.`;
}

export async function deliverProxyAlert(control: Control, env: Environment, request: typeof fetch = fetch): Promise<boolean> {
  const token = env.REPLAY_TELEGRAM_BOT_TOKEN, chat = env.REPLAY_TELEGRAM_CHAT_ID;
  if (!token && !chat) return false;
  if (!token || !/^\d{6,16}:[\w-]{25,80}$/.test(token) || !chat || !/^-?\d{1,20}$/.test(chat)) {
    throw new Error('Proxy alert configuration unavailable');
  }
  const { alert } = await control('proxy-alerts/claim') as { alert: ProxyAlert | null };
  if (!alert) return false;
  if (!/^[a-f0-9]{32}$/.test(alert.id) || !/^[a-f0-9]{32}$/.test(alert.lease_token)) throw new Error('Invalid proxy alert');
  // Never log the endpoint, HTTP body or caught error: Telegram embeds the bot
  // secret in its URL. A failed delivery retains the database lease for retry.
  try {
    const response = await request(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chat, text: proxyAlertText(alert),
        link_preview_options: { is_disabled: true },
        reply_markup: { inline_keyboard: [[{ text: '충전 페이지 열기', url: purchaseUrl }]] } }),
    });
    const result = await response.json() as { ok?: boolean };
    if (!response.ok || result.ok !== true) throw new Error('Delivery failed');
    const resultAck = await control('proxy-alerts/ack', { id: alert.id, lease_token: alert.lease_token }) as { acknowledged: boolean };
    if (!resultAck.acknowledged) throw new Error('Acknowledgement failed');
    return true;
  } catch { throw new Error('Proxy alert delivery unavailable'); }
}
