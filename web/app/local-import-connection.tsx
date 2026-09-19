'use client';

import { useState } from 'react';
import { Check, ChevronDown, LoaderCircle, Monitor, PlugZap } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import './local-import.css';

export default function LocalImportConnection({ connected, busy, disabled, onConnect, onDisconnect }: {
  connected: boolean; busy: boolean; disabled: boolean;
  onConnect: (code: string) => Promise<void>; onDisconnect: () => void;
}) {
  const [code, setCode] = useState('');
  async function connect() { await onConnect(code); setCode(''); }
  return <section className={`local-import-connection ${connected ? 'is-connected' : ''}`} aria-label="영상 가져오기 도우미">
    <div className="local-import-heading"><Monitor size={20} aria-hidden="true" /><div><strong>내 컴퓨터에서 가져오기</strong><p>도우미가 영상을 내려받아 내 보관함에 직접 업로드해요.</p></div>
      <span className="local-import-badge">{connected ? <><Check size={13} />연결됨</> : '연결 필요'}</span></div>
    {connected ? <div className="local-import-connected"><p>보관함에 저장될 때까지 이 창과 도우미를 켜두세요.</p><button type="button" disabled={disabled || busy} onClick={onDisconnect}>연결 해제</button></div>
      : <><div className="local-import-pair"><label htmlFor="local-import-code">도우미 연결 코드</label><div><Input id="local-import-code" type="password" autoComplete="off" spellCheck={false}
        placeholder="도우미에 표시된 코드" value={code} maxLength={64} disabled={disabled || busy}
        onChange={event => setCode(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); if (code.trim() && !busy && !disabled) void connect(); } }} />
        <Button type="button" variant="outline" disabled={disabled || busy || !code.trim()} onClick={() => void connect()}>{busy ? <LoaderCircle size={15} className="source-spinner" /> : <PlugZap size={15} />}내 컴퓨터 연결</Button></div></div>
        <details className="local-import-setup"><summary>처음 연결하나요?<ChevronDown size={14} /></summary><ol><li>이 컴퓨터의 Replay Live 프로젝트 폴더에서 도우미를 실행하세요.<code>.venv/bin/python scripts/local-import-daemon.py</code></li><li>실행 창에 표시된 연결 코드를 입력하세요.</li><li>브라우저가 내 컴퓨터 연결 권한을 요청하면 허용하세요.</li></ol><p>도우미는 이 컴퓨터에서만 접속할 수 있어요. 웹을 새로 열면 다시 연결해 주세요.</p></details></>}
  </section>;
}
