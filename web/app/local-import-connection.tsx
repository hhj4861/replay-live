'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Download, LoaderCircle, Monitor, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { helperRelease, type HelperRelease } from '@/lib/helper-release';
import './local-import.css';

export default function LocalImportConnection({ connected, disabled, onLoadCode, onConnect, onDisconnect }: {
  connected: boolean; disabled: boolean;
  onLoadCode: (signal: AbortSignal) => Promise<string>;
  onConnect: (code: string, signal: AbortSignal) => Promise<void>; onDisconnect: () => void;
}) {
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [paused, setPaused] = useState(false);
  const [release, setRelease] = useState<HelperRelease | null>(null);
  const [releaseError, setReleaseError] = useState('');
  const [launchRequested, setLaunchRequested] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const lastAttempt = useRef(0);
  const connect = useCallback(async (manualCode?: string) => {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller; lastAttempt.current = Date.now();
    try {
      await Promise.resolve();
      controller.signal.throwIfAborted();
      setLoading(true); setError('');
      const value = manualCode || await onLoadCode(controller.signal);
      controller.signal.throwIfAborted();
      setCode(value);
      await onConnect(value, controller.signal);
      controller.signal.throwIfAborted();
      setCode(''); setLaunchRequested(false);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '도우미 연결을 확인해 주세요.');
    } finally {
      if (pending.current === controller) { pending.current = null; setLoading(false); }
    }
  }, [onLoadCode, onConnect]);
  useEffect(() => {
    if (connected || disabled || paused) return;
    const initial = setTimeout(() => { void connect(); }, 0);
    const focus = () => { if (Date.now() - lastAttempt.current > 10_000) void connect(); };
    window.addEventListener('focus', focus);
    return () => { clearTimeout(initial); window.removeEventListener('focus', focus); pending.current?.abort(); pending.current = null; };
  }, [connected, disabled, paused, connect]);
  useEffect(() => {
    const controller = new AbortController();
    void helperRelease(controller.signal).then(setRelease, failure => {
      if (!controller.signal.aborted) setReleaseError(failure instanceof Error ? failure.message : '설치 파일 안내를 확인해 주세요.');
    });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!launchRequested || connected || disabled || paused) return;
    let attempts = 0;
    const timer = setInterval(() => {
      if (++attempts > 15) { clearInterval(timer); setLaunchRequested(false); }
      else void connect();
    }, 2000);
    return () => clearInterval(timer);
  }, [launchRequested, connected, disabled, paused, connect]);
  function retry() { setPaused(false); void connect(); }
  return <section className={`local-import-connection ${connected ? 'is-connected' : ''}`} aria-label="영상 가져오기 도우미">
    <div className="local-import-heading"><Monitor size={20} aria-hidden="true" /><div><strong>내 컴퓨터에서 영상 가져오기</strong><p>도우미가 이 PC에서 영상을 내려받아 로그인한 계정의 보관함에 업로드합니다.</p></div>
      <output className="local-import-badge">{connected ? <><Check size={13} />연결됨</> : loading ? '연결 확인 중' : paused ? '연결 해제됨' : '도우미 연결 필요'}</output></div>
    {connected ? <div className="local-import-connected"><p>영상 가져오기가 끝날 때까지 이 창과 도우미를 켜두세요.</p><button type="button" disabled={disabled} onClick={() => { setPaused(true); setCode(''); onDisconnect(); }}>연결 해제</button></div>
      : <div className="local-import-onboarding">
        <output>{loading ? '이 컴퓨터의 도우미를 찾아 자동으로 연결하고 있습니다.' : paused ? '자동 연결을 멈췄습니다. 다시 연결하려면 다시 확인을 누르세요.' : '처음 이용한다면 도우미를 설치하세요. 이미 설치했다면 도우미 실행을 눌러 주세요.'}</output>
        <div className="local-import-actions">
          {release?.downloads.map(item => <a key={item.label} href={item.url} rel="noreferrer"><Download size={15} />{item.label} 설치</a>)}
          <a href="replay-live-helper://start" aria-disabled={disabled} onClick={event => { if (disabled) { event.preventDefault(); return; } setPaused(false); setLaunchRequested(true); }}>도우미 실행</a>
          <Button type="button" variant="outline" disabled={disabled || loading} onClick={retry}>{loading ? <LoaderCircle size={15} className="source-spinner" /> : <RefreshCw size={15} />}다시 확인</Button>
        </div>
        {!release && <p className="hint">{releaseError || '설치 파일은 아직 배포 준비 중입니다. 설치된 도우미를 실행하거나 MP4 파일 업로드를 이용해 주세요.'}</p>}
        {launchRequested && <output>브라우저의 앱 열기를 허용해 주세요. 실행 후 자동으로 다시 연결합니다.</output>}
        {error && <p className="local-import-code-error" role="alert">{error} 설치 여부와 실행 상태를 확인하고, 브라우저가 이 컴퓨터 연결 권한을 요청하면 허용해 주세요.</p>}
        <details className="local-import-setup"><summary>설치와 자동 실행 안내<ChevronDown size={14} /></summary><ol><li>내 PC에 맞는 설치 파일을 내려받아 압축을 풀고 Replay Live Helper를 여세요.</li><li>설치 창에서 설치와 PC 로그인 시 자동 실행을 허용하세요. 설치가 끝나면 웹으로 돌아오세요.</li><li>이후 웹 로그인·새로고침 때 실행 중인 도우미와 자동으로 연결합니다.</li></ol><p>도우미 설정에서 자동 시작을 끄거나 제거할 수 있습니다. 연결 정보는 이 웹페이지의 메모리에만 두고 로그아웃·계정 변경 때 폐기합니다.</p></details>
        <details className="local-import-setup"><summary>연결 코드 직접 입력<ChevronDown size={14} /></summary><div className="local-import-pair"><label htmlFor="local-import-code">도우미 연결 코드</label><div><Input id="local-import-code" type="password" autoComplete="off" spellCheck={false} value={code} maxLength={64} disabled={disabled || loading} onChange={event => setCode(event.target.value)} />
          <Button type="button" variant="outline" disabled={disabled || loading || !code.trim()} onClick={() => { setPaused(false); void connect(code); }}>코드로 연결</Button></div></div></details>
      </div>}
  </section>;
}
