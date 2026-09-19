'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Check, KeyRound, LoaderCircle, Save } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { sessionIdentity } from '@/lib/api';
import type { StreamConnectionLocation, StreamConnectionMetadata, StreamConnectionValue } from '@/lib/stream-connections';
import { ConnectionFields, destinationReady, PlatformMark, type StreamTarget } from './platform-picker';

type Props = {
  identity: string; targets: StreamTarget[]; initialTarget?: string; connections: StreamConnectionMetadata[];
  location: StreamConnectionLocation; disabled: boolean;
  onLoad: (target: string) => Promise<StreamConnectionValue>;
  onSave: (target: string, value: StreamConnectionValue) => Promise<void>;
  onCancel: () => void; onSaved: (target: string) => void;
};

function ConnectionSteps({ current }: { current: 1 | 2 }) {
  return <ol className="member-editor-steps" aria-label="채널 연결 추가 단계">
    <li className={current === 1 ? 'is-current' : 'is-done'} aria-current={current === 1 ? 'step' : undefined}>
      <span aria-hidden="true">{current === 2 ? <Check size={12} /> : '1'}</span>플랫폼 선택</li>
    <li className={current === 2 ? 'is-current' : ''} aria-current={current === 2 ? 'step' : undefined}>
      <span aria-hidden="true">2</span>연결 정보</li>
  </ol>;
}

export default function ConnectionEditor(props: Props) {
  const [selected, setSelected] = useState(props.initialTarget || '');
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (!selected) heading.current?.focus(); }, [selected]);
  const choices = props.targets.filter(target => target.id !== 'local' && target.requires_stream_key);
  const target = choices.find(item => item.id === selected);
  if (target) return <ConnectionForm key={target.id} {...props} target={target}
    existing={props.connections.some(item => item.target === target.id)} onBack={() => setSelected('')} />;
  return <section className="member-connection-editor member-editor-select" aria-labelledby="connection-editor-title">
    <ConnectionSteps current={1} />
    <div className="member-editor-heading"><div><h3 ref={heading} tabIndex={-1} id="connection-editor-title">어느 플랫폼을 연결할까요?</h3>
      <p>방송할 플랫폼을 선택하세요. 플랫폼마다 연결 하나를 저장할 수 있어요.</p></div></div>
    <div className="member-platform-grid">{choices.map(choice => {
      const saved = props.connections.some(item => item.target === choice.id);
      return <button type="button" key={choice.id} disabled={props.disabled} aria-label={`${choice.label} ${saved ? '연결 수정' : '연결 추가'}`}
        onClick={() => setSelected(choice.id)}><PlatformMark id={choice.id} /><span><strong>{choice.label}</strong><small className={saved ? 'is-saved' : undefined}>{saved ? <><Check size={12} />저장한 연결 수정</> : '새 연결 추가'}</small></span></button>;
    })}</div>
    {!choices.length && <output className="member-subtle">플랫폼 목록을 준비하고 있어요. 잠시 후 다시 시도해 주세요.</output>}
    <div className="member-editor-footer"><p>이미 저장한 플랫폼을 선택하면 연결 정보를 수정할 수 있어요.</p><Button variant="outline" onClick={props.onCancel}>취소</Button></div>
  </section>;
}

function ConnectionForm(props: Props & { target: StreamTarget; existing: boolean; onBack: () => void }) {
  const { identity, target } = props;
  // Freeze the user's edit intent; metadata polling must not replace their draft.
  const [existing] = useState(props.existing);
  const [draft, setDraft] = useState<StreamConnectionValue>({ server_url: target.default_server_url || '', stream_key: '' });
  const [loading, setLoading] = useState(existing);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const alive = useRef(false);
  const savePending = useRef(false);
  const callbacks = useRef(props);
  useEffect(() => { callbacks.current = props; });
  useEffect(() => { alive.current = true; heading.current?.focus(); return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!existing) return;
    let active = true;
    void callbacks.current.onLoad(target.id).then(value => {
      if (active && identity === sessionIdentity()) setDraft({ server_url: value.server_url, stream_key: value.stream_key, channel_url: value.channel_url || '' });
    }).catch(() => {
      if (active && identity === sessionIdentity()) setLoadError('저장한 연결을 불러오지 못했습니다. 다시 시도해 주세요.');
    }).finally(() => { if (active && identity === sessionIdentity()) setLoading(false); });
    return () => { active = false; };
  }, [identity, target.id, existing, revision]);
  const current = () => alive.current && identity === sessionIdentity();
  const ready = destinationReady(target, draft);
  async function save() {
    if (!current() || savePending.current || loading || loadError || props.disabled || !ready) return;
    savePending.current = true; setSaving(true); setError('');
    try {
      await props.onSave(target.id, { server_url: draft.server_url, stream_key: draft.stream_key, channel_url: draft.channel_url || '' });
      if (current()) { setDraft({ server_url: '', stream_key: '' }); props.onSaved(target.id); }
    } catch {
      if (current()) setError('연결 정보를 저장하지 못했습니다. 입력한 내용은 유지됩니다. 연결 상태를 확인한 뒤 다시 저장해 주세요.');
    } finally { savePending.current = false; if (current()) setSaving(false); }
  }
  return <section className="member-connection-editor member-editor-form" aria-labelledby="connection-editor-title">
    {!props.initialTarget && <ConnectionSteps current={2} />}
    <div className="member-editor-heading"><div><h3 ref={heading} tabIndex={-1} id="connection-editor-title">{target.label} 연결 {existing ? '수정' : '추가'}</h3>
      <p>{existing ? '저장한 정보를 확인하고 변경한 내용만 수정하세요.' : '플랫폼의 라이브 설정에서 스트림 키를 복사해 입력하세요.'}</p></div>
      {!props.initialTarget && <Button variant="ghost" disabled={saving} onClick={props.onBack}><ArrowLeft size={14} />플랫폼 변경</Button>}</div>
    {loading ? <output className="member-loading"><LoaderCircle size={18} className="source-spinner" />저장한 연결을 불러오는 중…</output>
      : loadError ? <div className="message error" role="alert">{loadError}<Button variant="outline" disabled={props.disabled} onClick={() => { setLoading(true); setLoadError(''); setRevision(value => value + 1); }}>다시 불러오기</Button></div>
      : <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <ConnectionFields target={target} destination={draft} purpose="connection" disabled={props.disabled || saving}
          onChange={value => { setDraft({ server_url: value.server_url, stream_key: value.stream_key, channel_url: value.channel_url || '' }); setError(''); }} />
        <p className="member-editor-storage"><KeyRound size={15} aria-hidden="true" /><span><strong>{props.location === 'browser' ? '이 브라우저에 암호화 저장' : '내 계정에 암호화 저장'}</strong>저장한 연결은 다음 방송에서 사용할 수 있어요.</span></p>
        {error && <p className="message error" role="alert">{error}</p>}
        <div className="member-editor-footer"><p>{!ready ? '스트림 키와 송출 서버 URL을 확인해 주세요.' : '연결 정보가 준비됐어요. 저장 후 방송에 사용할 수 있습니다.'}</p>
          <Button type="button" variant="outline" disabled={saving} onClick={props.onCancel}>취소</Button>
          <Button type="submit" className="member-add-connection" disabled={props.disabled || saving || !ready}>{saving ? <LoaderCircle size={15} className="source-spinner" /> : <Save size={15} />}{saving ? '저장 중…' : '연결 저장'}</Button></div>
      </form>}
    {(loading || loadError) && <div className="member-editor-footer"><Button variant="outline" onClick={props.onCancel}>취소</Button></div>}
  </section>;
}
