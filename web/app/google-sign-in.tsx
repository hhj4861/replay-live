'use client';

import { useEffect, useRef, useState } from 'react';
import { beginGoogleLogin, cancelGoogleLogin, finishGoogleLogin, googleChallenge, GOOGLE_CLIENT_ID } from '@/lib/google-auth';

type GoogleIdentity = { initialize: (options: { client_id: string; nonce: string; callback: (value: { credential: string }) => void;
  auto_select: boolean; use_fedcm_for_button: boolean; ux_mode: string }) => void;
  renderButton: (element: HTMLElement, options: Record<string, string | number>) => void };
declare global { interface Window { google?: { accounts: { id: GoogleIdentity } } } }
let sdk: Promise<GoogleIdentity> | undefined;
function loadGoogle() {
  if (window.google?.accounts.id) return Promise.resolve(window.google.accounts.id);
  if (!sdk) sdk = new Promise<GoogleIdentity>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://accounts.google.com/gsi/client'; script.async = true; script.defer = true;
    const timer = setTimeout(() => { script.remove(); reject(new Error('Google 로그인 버튼을 불러오지 못했습니다. 다시 시도하세요.')); }, 15000);
    script.onload = () => { clearTimeout(timer); const identity = window.google?.accounts.id;
      if (identity) resolve(identity); else { script.remove(); reject(new Error('Google 로그인 연결을 확인하세요.')); } };
    script.onerror = () => { clearTimeout(timer); script.remove(); reject(new Error('Google에 연결할 수 없습니다. 다시 시도하세요.')); };
    document.head.appendChild(script);
  }).catch(error => { sdk = undefined; throw error; });
  return sdk;
}

export default function GoogleSignIn({ onSuccess, developmentPreview = false }: { onSuccess: () => void; developmentPreview?: boolean }) {
  const container = useRef<HTMLDivElement>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!GOOGLE_CLIENT_ID) return;
    let active = true; let submitting = false; let timer: ReturnType<typeof setTimeout> | undefined;
    const attempt = beginGoogleLogin();
    void Promise.all([loadGoogle(), googleChallenge()]).then(([identity, challenge]) => {
      if (!active || !container.current) return;
      identity.initialize({ client_id: GOOGLE_CLIENT_ID, nonce: challenge.nonce, auto_select: false,
        use_fedcm_for_button: true, ux_mode: 'popup', callback: response => {
          if (!active || submitting) return;
          submitting = true;
          setBusy(true); setError('');
          void finishGoogleLogin(response.credential, challenge, attempt).then(() => { if (active) onSuccess(); })
            .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : '로그인을 다시 시도하세요.'); })
            .finally(() => { if (active) setBusy(false); });
        } });
      container.current.replaceChildren();
      identity.renderButton(container.current, { type: 'standard', theme: 'outline', size: 'large', text: 'continue_with',
        shape: 'rectangular', locale: 'ko', width: Math.min(320, container.current.clientWidth || 320) });
      setLoaded(true);
      timer = setTimeout(() => { if (active) { setError('로그인 요청 시간이 지났습니다. 다시 시도하세요.'); cancelGoogleLogin(attempt); } },
        Math.max(0, challenge.expires_at * 1000 - Date.now()));
    }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'Google 로그인 연결을 확인하세요.'); });
    return () => { active = false; if (timer) clearTimeout(timer); cancelGoogleLogin(attempt); };
    // A fresh attempt owns its nonce and callback until success, retry or unmount.
  }, [retry, onSuccess]);
  return <div className="google-login">
    {!GOOGLE_CLIENT_ID ? <div className="google-unavailable"><strong>Google 로그인 미연결</strong><p className="hint">{developmentPreview
      ? '이 화면은 개발용 미리보기입니다. 아래 버튼으로 화면을 확인할 수 있으며 Google 계정은 사용하지 않습니다.'
      : 'Google 로그인 설정이 아직 완료되지 않았습니다. 관리자에게 문의하세요.'}</p></div> : <>
      <div ref={container} className={busy || error ? 'google-button unavailable' : 'google-button'} aria-busy={busy} />
      {!loaded && !error && <output className="hint">Google 로그인 연결 중…</output>}
      {busy && <output className="hint">계정 확인 중…</output>}
      {error && <><p className="message error" role="alert">{error}</p><button type="button" className="login-retry" onClick={() => { setError(''); setLoaded(false); setBusy(false); setRetry(value => value + 1); }}>다시 시도</button></>}
    </>}
  </div>;
}
