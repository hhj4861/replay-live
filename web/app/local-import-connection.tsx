'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, LoaderCircle, RefreshCw } from 'lucide-react';
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
    });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => { void connect(); }, 2000);
    const timeout = setTimeout(() => { setWaiting(false); setError('자동 연결 대기 시간이 지났습니다. 도우미 실행 후 다시 연결을 눌러 주세요.'); }, 5 * 60_000);
    return () => { clearInterval(timer); clearTimeout(timeout); };
  }, [waiting, connect]);
  function cancel(upload = false) {
    closed.current = true; pending.current?.abort();
    if (upload) onUpload(); else onClose();
  }
  function confirmInstall() {
    if (!selected || closed.current) return;
    if (requestHelperDownload(selected)) { setDownloadRequested(true); setWaiting(true); }
    else setError('설치 파일 다운로드를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.');
  }
  return <dialog ref={dialog} className="helper-install-dialog" aria-labelledby="helper-install-title" aria-describedby="helper-install-description"
    onCancel={event => { event.preventDefault(); cancel(); }}>
    <h2 id="helper-install-title">링크 다운로드에 도우미가 필요해요</h2>
    <p id="helper-install-description">도우미가 이 PC에서 영상을 내려받아 내 보관함에 업로드합니다. 설치 후 자동으로 연결하고 방금 요청한 링크를 계속 가져옵니다.</p>
    <output aria-live="polite">{loading ? <><LoaderCircle size={16} className="source-spinner" />실행 중인 도우미를 확인하고 있습니다.</> : waiting ? '설치와 실행을 마치면 자동으로 이어집니다. 이 창을 열어 두세요.' : '설치된 도우미가 있다면 아래에서 실행해 주세요.'}</output>
    <p>{platform?.message || '이 PC에 맞는 설치 파일을 확인하고 있습니다.'}</p>
    {selected && <p>설치 파일: <strong>{selected.label}</strong></p>}
    {!release && <output>{releaseError || '설치 파일은 아직 배포 준비 중입니다. 설치된 도우미를 실행하거나 MP4 파일을 업로드해 주세요.'}</output>}
    {release && platform?.label && !selected && <output>이 PC용 설치 파일은 아직 준비 중입니다. MP4 파일을 업로드해 주세요.</output>}
    {selected && <p>동의하면 설치 파일을 다운로드합니다. 내려받은 파일을 열고 운영체제의 설치·실행 승인을 완료해 주세요. 연결 코드나 PC 정보는 입력하지 않아도 됩니다.</p>}
    {downloadRequested && <output>다운로드를 요청했습니다. 파일을 열어 설치해 주세요. 시작되지 않았다면 다운로드를 다시 누르세요.</output>}
    {error && <p role="alert">{error} 브라우저가 내 컴퓨터 연결 권한을 요청하면 허용해 주세요.</p>}
    <div className="local-import-actions">
      <Button type="button" variant="outline" onClick={() => cancel()}>취소</Button>
      <Button type="button" variant="outline" onClick={() => cancel(true)}>MP4 파일 업로드</Button>
      <Button type="button" disabled={!selected} onClick={confirmInstall}><Download size={15} />{downloadRequested ? '설치 파일 다시 다운로드' : '동의하고 설치 파일 다운로드'}</Button>
    </div>
    <div className="local-import-actions">
      <a href="replay-live-helper://start" onClick={() => setWaiting(true)}>설치된 도우미 실행</a>
      <Button type="button" variant="ghost" disabled={loading} onClick={() => void connect()}><RefreshCw size={15} />다시 연결</Button>
    </div>
  </dialog>;
}
