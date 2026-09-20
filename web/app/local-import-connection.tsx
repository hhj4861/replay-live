'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, LoaderCircle, Monitor, PlugZap, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import './local-import.css';

export default function LocalImportConnection({ connected, busy, disabled, onLoadCode, onConnect, onDisconnect }: {
  connected: boolean; busy: boolean; disabled: boolean;
  onLoadCode: (signal: AbortSignal) => Promise<string>;
  onConnect: (code: string) => Promise<void>; onDisconnect: () => void;
}) {
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(true);
  const [codeError, setCodeError] = useState('');
  const [codeNotice, setCodeNotice] = useState('');
  const pending = useRef<AbortController | null>(null);
  const loadCode = useCallback(() => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    return onLoadCode(controller.signal).then(value => {
      if (!controller.signal.aborted) { setCode(value); setCodeNotice('이 컴퓨터의 연결 코드를 불러왔습니다. 내 컴퓨터 연결을 눌러 주세요.'); }
    }, () => {
      if (!controller.signal.aborted) setCodeError('연결 코드를 불러오지 못했습니다. 최신 도우미를 실행하고 브라우저의 내 컴퓨터 연결 권한을 허용한 뒤 다시 불러오세요. 실행 창의 코드를 직접 입력할 수도 있습니다.');
    }).finally(() => {
      if (!controller.signal.aborted) { pending.current = null; setLoading(false); }
    });
  }, [onLoadCode]);
  function resetCode() { setLoading(true); setCode(''); setCodeError(''); setCodeNotice(''); }
  function reloadCode() { resetCode(); void loadCode(); }
  useEffect(() => {
    if (!connected) void loadCode();
    return () => { pending.current?.abort(); };
  }, [connected, loadCode]);
  async function connect() { await onConnect(code); }
  return <section className={`local-import-connection ${connected ? 'is-connected' : ''}`} aria-label="영상 가져오기 도우미">
    <div className="local-import-heading"><Monitor size={20} aria-hidden="true" /><div><strong>내 컴퓨터에서 가져오기</strong><p>도우미가 영상을 내려받아 내 보관함에 직접 업로드해요.</p></div>
      <span className="local-import-badge">{connected ? <><Check size={13} />연결됨</> : '연결 필요'}</span></div>
    {connected ? <div className="local-import-connected"><p>보관함에 저장될 때까지 이 창과 도우미를 켜두세요.</p><button type="button" disabled={disabled || busy} onClick={() => { resetCode(); onDisconnect(); }}>연결 해제</button></div>
      : <><div className="local-import-pair"><label htmlFor="local-import-code">도우미 연결 코드</label><div><Input id="local-import-code" type="password" autoComplete="off" spellCheck={false}
        placeholder={loading ? '도우미에서 코드를 불러오는 중' : '자동으로 불러오거나 직접 입력'} value={code} maxLength={64} disabled={disabled || busy}
        aria-describedby="local-import-code-status"
        onChange={event => { pending.current?.abort(); pending.current = null; setLoading(false); setCodeError(''); setCodeNotice(''); setCode(event.target.value); }}
        onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); if (code.trim() && !busy && !disabled && !loading) void connect(); } }} />
        <Button type="button" variant="outline" disabled={disabled || busy || loading} onClick={reloadCode}>{loading ? <LoaderCircle size={15} className="source-spinner" /> : <RefreshCw size={15} />}코드 불러오기</Button>
        <Button type="button" variant="outline" disabled={disabled || busy || loading || !code.trim()} onClick={() => void connect()}>{busy ? <LoaderCircle size={15} className="source-spinner" /> : <PlugZap size={15} />}내 컴퓨터 연결</Button></div>
        <p id="local-import-code-status" className={codeError ? 'local-import-code-error' : 'local-import-code-status'} role={codeError ? 'alert' : 'status'}>{codeError || (loading ? '이 컴퓨터에서 실행 중인 도우미를 찾고 있습니다.' : codeNotice || '도우미를 켜면 이 컴퓨터의 코드를 불러올 수 있습니다.')}</p></div>
        <details className="local-import-setup"><summary>처음 연결하나요?<ChevronDown size={14} /></summary><ol><li>이 컴퓨터의 Replay Live 프로젝트 폴더에서 최신 도우미를 실행하세요.<code>.venv/bin/python scripts/local-import-daemon.py</code></li><li>브라우저가 내 컴퓨터 연결 권한을 요청하면 허용하세요.</li><li>코드가 자동으로 채워지면 내 컴퓨터 연결을 누르세요. 비어 있다면 코드 불러오기를 누르거나 실행 창의 코드를 입력하세요.</li></ol><p>새로고침·재로그인하면 실행 중인 도우미의 현재 코드를 다시 불러옵니다. 코드는 이 PC의 도우미를 재시작할 때 바뀌며 브라우저에 저장하지 않습니다.</p></details></>}
  </section>;
}
