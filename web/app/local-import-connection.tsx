'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Download, LoaderCircle, Monitor, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { helperRelease, type HelperRelease } from '@/lib/helper-release';
import { detectHelperPlatform, platformDownloads, requestHelperDownload, type HelperPlatform } from '@/lib/helper-platform';
import './local-import.css';

export default function LocalImportConnection({ connected, disabled, onLoadCode, onConnect, onDisconnect, installOpen = false, onInstallClose }: {
  connected: boolean; disabled: boolean; installOpen?: boolean; onInstallClose?: () => void;
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
  const [platform, setPlatform] = useState<HelperPlatform | null>(null);
  const [chosenLabel, setChosenLabel] = useState('');
  const [showInstall, setShowInstall] = useState(false);
  const installDialog = useRef<HTMLDialogElement | null>(null);
  const installTrigger = useRef<HTMLButtonElement | null>(null);
  const [downloadRequested, setDownloadRequested] = useState(false);
  const downloads = platformDownloads(release, platform);
  const selected = downloads.find(item => item.label === (chosenLabel || platform?.label));
  const pending = useRef<AbortController | null>(null);
  const lastAttempt = useRef(0);
  const connect = useCallback(async (manualCode?: string, offerInstall = false) => {
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
      setCode(''); setLaunchRequested(false); setShowInstall(false);
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(failure instanceof Error ? failure.message : '도우미 연결을 확인해 주세요.');
        if (offerInstall) setShowInstall(true);
      }
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
    let active = true;
    void detectHelperPlatform().then(value => { if (active) setPlatform(value); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    const dialog = installDialog.current;
    if (!dialog) return;
    if ((installOpen || showInstall) && !connected) { if (!dialog.open) dialog.showModal(); }
    else if (dialog.open) dialog.close();
  }, [installOpen, showInstall, connected]);
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
  function closeInstall() { setShowInstall(false); onInstallClose?.(); }
  function confirmInstall() {
    if (disabled || !selected) return;
    if (requestHelperDownload(selected)) {
      setDownloadRequested(true); setPaused(false); setLaunchRequested(true);
    } else setError('설치 파일 다운로드를 시작하지 못했습니다. 잠시 후 다시 확인해 주세요.');
  }
  const deviceChoice = release && platform && platform.os !== 'unsupported' && !platform.label && downloads.length > 0
    ? <label>PC 종류 <select aria-label="도우미를 설치할 PC 종류" value={chosenLabel} disabled={disabled} onChange={event => setChosenLabel(event.target.value)}>
      <option value="">PC 종류 선택</option>{downloads.map(item => <option key={item.label} value={item.label}>{item.label}</option>)}
    </select></label> : null;
  function retry(event?: { currentTarget: HTMLButtonElement }) { installTrigger.current = event?.currentTarget || null; setPaused(false); void connect(undefined, true); }
  return <section className={`local-import-connection ${connected ? 'is-connected' : ''}`} aria-label="영상 가져오기 도우미">
    <div className="local-import-heading"><Monitor size={20} aria-hidden="true" /><div><strong>도우미 데몬 사용</strong><p>도우미가 이 PC에서 영상을 내려받아 로그인한 계정의 보관함에 업로드합니다.</p></div>
      <output className="local-import-badge">{connected ? <><Check size={13} />연결됨</> : loading ? '연결 확인 중' : paused ? '연결 해제됨' : '도우미 연결 필요'}</output></div>
    {connected ? <div className="local-import-connected"><p>영상 가져오기가 끝날 때까지 이 창과 도우미를 켜두세요.</p><button type="button" disabled={disabled} onClick={() => { setPaused(true); setCode(''); onDisconnect(); }}>연결 해제</button></div>
      : <div className="local-import-onboarding">
        <output>{loading ? '이 컴퓨터의 도우미를 찾아 자동으로 연결하고 있습니다.' : paused ? '자동 연결을 멈췄습니다. 다시 연결하려면 도우미 연결을 누르세요.' : '도우미를 먼저 연결해 주세요. 연결이 완료되면 영상과 플랫폼 정보를 입력할 수 있습니다.'}</output>
        <p>{platform?.message || '이 PC의 운영체제와 CPU 정보를 확인하고 있습니다.'}</p>
        <div className="local-import-actions">
          <Button type="button" disabled={disabled || loading} onClick={retry}>{loading ? <LoaderCircle size={15} className="source-spinner" /> : <RefreshCw size={15} />}도우미 연결</Button>
          <Button type="button" variant="outline" disabled={disabled} onClick={event => { installTrigger.current = event.currentTarget; setShowInstall(true); }}><Download size={15} />도우미 설치 안내</Button>
          <a href="replay-live-helper://start" aria-disabled={disabled} onClick={event => { if (disabled) { event.preventDefault(); return; } setPaused(false); setLaunchRequested(true); }}>도우미 실행</a>
        </div>
        {downloadRequested && <output>설치 파일 다운로드를 요청했습니다. 시작되지 않았다면 설치 안내에서 다시 다운로드해 주세요. 내려받은 파일을 열어 설치를 승인하면 자동으로 연결됩니다.</output>}
        {release && platform?.label && !selected && <p>이 PC용 설치 파일은 아직 준비 중입니다.</p>}
        {!release && <p className="hint">{releaseError || '설치 파일은 아직 배포 준비 중입니다. 설치된 도우미를 실행한 뒤 연결해 주세요.'}</p>}
        {launchRequested && <output>브라우저의 앱 열기를 허용해 주세요. 실행 후 자동으로 다시 연결합니다.</output>}
        {error && <p className="local-import-code-error" role="alert">{error} 설치 여부와 실행 상태를 확인하고, 브라우저가 이 컴퓨터 연결 권한을 요청하면 허용해 주세요.</p>}
        <details className="local-import-setup"><summary>설치와 자동 실행 안내<ChevronDown size={14} /></summary><ol><li>자동 선택된 설치 파일을 내려받아 압축을 풀고 Replay Live Helper를 여세요. 웹페이지는 프로그램 설치를 직접 승인할 수 없습니다.</li><li>설치 창에서 설치와 PC 로그인 시 자동 실행을 허용하세요. 설치가 끝나면 웹으로 돌아오세요.</li><li>이후 웹 로그인·새로고침 때 실행 중인 도우미와 자동으로 연결합니다.</li></ol><p>도우미 설정에서 자동 시작을 끄거나 제거할 수 있습니다. 연결 정보는 이 웹페이지의 메모리에만 두고 로그아웃·계정 변경 때 폐기합니다.</p></details>
        <details className="local-import-setup"><summary>연결 코드 직접 입력<ChevronDown size={14} /></summary><div className="local-import-pair"><label htmlFor="local-import-code">도우미 연결 코드</label><div><Input id="local-import-code" type="password" autoComplete="off" spellCheck={false} value={code} maxLength={64} disabled={disabled || loading} onChange={event => setCode(event.target.value)} />
          <Button type="button" variant="outline" disabled={disabled || loading || !code.trim()} onClick={() => { setPaused(false); void connect(code); }}>코드로 연결</Button></div></div></details>
      </div>}
    <dialog ref={installDialog} className="helper-install-dialog" aria-labelledby="helper-install-title" aria-describedby="helper-install-description" onCancel={closeInstall} onClose={() => { closeInstall(); if (!connected) installTrigger.current?.focus(); }}>
      <h2 id="helper-install-title">영상 가져오기에 도우미가 필요합니다</h2>
      <p id="helper-install-description">이 PC에서 영상을 내려받으려면 Replay Live 도우미를 설치하고 실행해야 합니다. 이미 설치했다면 도우미 실행을 눌러 주세요.</p>
      <p>{platform?.message || 'PC 정보를 확인하고 있습니다.'}</p>
      {deviceChoice}
      {selected && <p>선택한 설치 파일: <strong>{selected.label}</strong></p>}
      {!release && <output>{releaseError || '설치 파일은 아직 배포 준비 중입니다. 현재는 설치된 도우미를 실행한 뒤 연결해 주세요.'}</output>}
      {release && platform?.label && !selected && <output>이 PC용 설치 파일은 아직 준비 중입니다.</output>}
      <p>확인을 누르면 설치 파일을 다운로드합니다. 내려받은 파일을 열어 설치와 자동 시작을 승인한 뒤 이 페이지로 돌아오세요. 도우미가 연결되면 영상과 플랫폼 정보를 입력할 수 있습니다.</p>
      {downloadRequested && <output>다운로드를 요청했습니다. 파일을 받은 뒤 실행해 주세요. 시작되지 않았다면 확인을 다시 누르세요.</output>}
      {error && <p role="alert">{error}</p>}
      <div className="local-import-actions">
        <Button type="button" variant="outline" onClick={closeInstall}>취소</Button>
        <a href="replay-live-helper://start" aria-disabled={disabled} onClick={event => { if (disabled) { event.preventDefault(); return; } setPaused(false); setLaunchRequested(true); }}>도우미 실행</a>
        <Button type="button" disabled={disabled || !selected} onClick={confirmInstall}>확인 · 설치 파일 다운로드</Button>
      </div>
    </dialog>
  </section>;
}
