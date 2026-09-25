'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, CircleHelp, Download, ExternalLink, LoaderCircle, Monitor, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { helperRelease, type HelperRelease } from '@/lib/helper-release';
import { detectHelperPlatform, platformDownloads, requestHelperDownload, type HelperPlatform } from '@/lib/helper-platform';
import './local-import.css';

// Mounted only by a link-download request. Login and ordinary studio work must
// never probe loopback, ask for hardware hints or start an installer download.
export default function LocalImportConnection({ onLoadCode, onConnect, onClose, onUpload }: {
  onLoadCode: (signal: AbortSignal) => Promise<string>;
  onConnect: (code: string, signal: AbortSignal) => Promise<void>;
  onClose: () => void; onUpload: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [release, setRelease] = useState<HelperRelease | null>(null);
  const [releaseError, setReleaseError] = useState('');
  const [releaseLoading, setReleaseLoading] = useState(true);
  const [platform, setPlatform] = useState<HelperPlatform | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [downloadRequested, setDownloadRequested] = useState(false);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const pending = useRef<AbortController | null>(null);
  const closed = useRef(false);
  const selected = platformDownloads(release, platform).find(item => item.label === platform?.label);
  const connect = useCallback(async () => {
    if (closed.current || pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setLoading(true);
    try {
      const code = await onLoadCode(controller.signal);
      controller.signal.throwIfAborted();
      await onConnect(code, controller.signal);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '도우미 연결을 확인해 주세요.');
    } finally {
      if (pending.current === controller) { pending.current = null; setLoading(false); }
    }
  }, [onLoadCode, onConnect]);
  useEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closed.current = false;
    element?.showModal();
    return () => { closed.current = true; pending.current?.abort(); element?.close(); requestAnimationFrame(() => { if (trigger?.isConnected) trigger.focus(); }); };
  }, []);
  useEffect(() => {
    const initial = setTimeout(() => { void connect(); }, 0);
    const focus = () => { void connect(); };
    window.addEventListener('focus', focus);
    return () => { clearTimeout(initial); window.removeEventListener('focus', focus); pending.current?.abort(); pending.current = null; };
  }, [connect]);
  useEffect(() => {
    const controller = new AbortController();
    void detectHelperPlatform().then(value => { if (!controller.signal.aborted) setPlatform(value); });
    void helperRelease(controller.signal).then(value => { if (!controller.signal.aborted) setRelease(value); }, failure => {
      if (!controller.signal.aborted) setReleaseError(failure instanceof Error ? failure.message : '설치 파일 안내를 확인해 주세요.');
    }).finally(() => { if (!controller.signal.aborted) setReleaseLoading(false); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => { void connect(); }, 2000);
    const timeout = setTimeout(() => { setWaiting(false); setError('아직 연결되지 않았어요. 도우미가 실행 중인지 확인하고 다시 시도해 주세요.'); }, 5 * 60_000);
    return () => { clearInterval(timer); clearTimeout(timeout); };
  }, [waiting, connect]);
  function cancel(upload = false) {
    closed.current = true; pending.current?.abort();
    if (upload) onUpload(); else onClose();
  }
  function startHelper() { setError(''); setWaiting(true); }
  function retry() { setError(''); void connect(); }
  function confirmInstall() {
    if (!selected || closed.current) return;
    if (requestHelperDownload(selected)) { setError(''); setDownloadRequested(true); setWaiting(true); }
    else setError('설치 파일 다운로드를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.');
  }
  const unsupported = platform?.os === 'unsupported';
  const checking = !platform || releaseLoading;
  const connectionUnavailable = error === '영상 가져오기 도우미를 실행하고 브라우저의 내 컴퓨터 연결 권한을 허용해 주세요.';
  const visibleError = connectionUnavailable ? (waiting ? '' : '실행 중인 도우미에 연결하지 못했어요.') : error;
  return <dialog ref={dialog} className="helper-install-dialog" aria-labelledby="helper-install-title" aria-describedby="helper-install-description"
    onCancel={event => { event.preventDefault(); cancel(); }}>
    <header className="helper-dialog-heading">
      <span className="helper-dialog-icon"><Monitor size={22} aria-hidden="true" /></span>
      <button type="button" className="helper-dialog-close" aria-label="도우미 연결 닫기" onClick={() => cancel()}><X size={20} aria-hidden="true" /></button>
    </header>
    <h2 id="helper-install-title">{unsupported ? 'MP4 파일로 가져와 주세요' : '도우미를 연결해 주세요'}</h2>
    <p id="helper-install-description">{unsupported ? '이 기기에서는 도우미를 사용할 수 없어요.' : '이 PC에서 영상을 내려받아 내 보관함에 저장해요.'}</p>
    <div className="helper-dialog-body">
      {checking ? <output className="helper-dialog-status" aria-live="polite"><LoaderCircle size={16} className="source-spinner" aria-hidden="true" />이 PC에 맞는 방법을 확인하고 있어요.</output> : <>
        {selected && <span className="helper-device"><Monitor size={14} aria-hidden="true" />{selected.label}</span>}
        {!unsupported && <ol className="helper-connect-steps" aria-label="도우미 연결 순서">
          {selected && (!waiting || downloadRequested) ? <>
            <li className={downloadRequested ? 'is-complete' : 'is-current'} aria-current={!downloadRequested ? 'step' : undefined}><span>{downloadRequested ? <Check size={14} aria-hidden="true" /> : '1'}</span><div><strong>설치 파일 다운로드</strong></div></li>
            <li className={downloadRequested ? 'is-current' : ''} aria-current={downloadRequested ? 'step' : undefined}><span>2</span><div><strong>내려받은 파일 열어 설치</strong>{downloadRequested && <p>운영체제의 설치·실행 안내를 따라 주세요.</p>}</div></li>
            <li><span>3</span><div><strong>연결되면 영상 가져오기 시작</strong></div></li>
          </> : <>
            <li className="is-current" aria-current="step"><span>1</span><div><strong>설치된 도우미 실행</strong></div></li>
            <li><span>2</span><div><strong>연결되면 영상 가져오기 시작</strong></div></li>
          </>}
        </ol>}
        {!selected && !unsupported && <p className="helper-availability">{releaseError || (platform?.label ? '새 설치 파일은 준비 중이에요. 이미 설치했다면 바로 실행할 수 있어요.' : platform?.message)}</p>}
        {visibleError && !unsupported && <p className="helper-dialog-error" role="alert">{visibleError}</p>}
        <div className="helper-primary-action">
          {unsupported ? <Button type="button" onClick={() => cancel(true)}>MP4 파일 업로드</Button>
            : waiting ? <output className="helper-dialog-status" aria-live="polite"><LoaderCircle size={16} className="source-spinner" aria-hidden="true" />{downloadRequested ? '설치 후 자동으로 연결돼요. 이 창을 열어 두세요.' : '도우미가 열리면 자동으로 연결돼요.'}</output>
            : selected ? <Button type="button" onClick={confirmInstall}><Download size={16} aria-hidden="true" />동의하고 다운로드</Button>
            : <a href="replay-live-helper://start" onClick={startHelper}>도우미 실행<ExternalLink size={16} aria-hidden="true" /></a>}
          {selected && !waiting && <a className="helper-existing-link" href="replay-live-helper://start" onClick={startHelper}>이미 설치했어요 · 도우미 실행</a>}
        </div>
      </>}
      {!unsupported && <details className="helper-dialog-help">
        <summary><CircleHelp size={15} aria-hidden="true" />연결이 안 되나요?</summary>
        <div>
          <p>브라우저가 앱 열기나 내 컴퓨터 연결 권한을 요청하면 허용해 주세요. 도우미가 실행 중이면 연결을 다시 확인할 수 있어요.</p>
          <button type="button" disabled={loading} onClick={retry}>다시 연결</button>
          {selected && downloadRequested && <button type="button" onClick={confirmInstall}>설치 파일 다시 다운로드</button>}
        </div>
      </details>}
    </div>
    {!unsupported && <footer className="helper-dialog-footer"><span>도우미 없이 진행하려면</span><button type="button" onClick={() => cancel(true)}>MP4 파일 업로드</button></footer>}
  </dialog>;
}
