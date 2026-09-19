'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Check, ChevronLeft, ChevronRight, KeyRound, LoaderCircle, LogOut, Radio,
  RefreshCw, Search, ShieldCheck, Trash2, UserRound, Users, Plus, Pencil } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { sessionIdentity } from '@/lib/api';
import { canManageMembers, createMemberManagementClient, type Account, type MemberDetail,
  type MemberManagementClient, type MemberPage } from '@/lib/member-management';
import type { StreamConnectionMetadata, StreamConnectionLocation, StreamConnectionValue } from '@/lib/stream-connections';
import ConnectionEditor from './connection-editor';
import { PlatformMark, type StreamTarget } from './platform-picker';
import './member-management.css';

type Props = {
  mode: 'account' | 'members'; identity: string; account: Account; targets: StreamTarget[];
  connections: StreamConnectionMetadata[]; location: StreamConnectionLocation; connectionError: string;
  canEditConnections: boolean; disabled: boolean; onClose: () => void; onUseConnection: (target: string) => void;
  onRemoveConnection: (target: string) => Promise<void>; onOwnConnectionRemovalStarted: (target: string) => void;
  onOwnConnectionDeleted: (target: string) => void; onSessionRevoked: () => void;
  onLoadConnection: (target: string) => Promise<StreamConnectionValue>;
  onSaveConnection: (target: string, value: StreamConnectionValue) => Promise<void>;
};
const when = (value: number) => Number.isFinite(value) && value > 0
  ? new Date(value * 1000).toLocaleString('ko-KR', { dateStyle: 'medium', timeStyle: 'short' }) : '정보 없음';
const roleName = (roles: string[]) => roles.includes('site_admin') ? '사이트 관리자'
  : roles.includes('admin') ? '계정 관리자' : roles.includes('operator') ? '방송 운영자' : '조회 회원';

function ConfirmAction({ title, description, label, busy, onCancel, onConfirm }: {
  title: string; description: string; label: string; busy: boolean; onCancel: () => void; onConfirm: () => void;
}) {
  const container = useRef<HTMLFieldSetElement>(null);
  useEffect(() => { container.current?.focus(); }, [title]);
  return <fieldset ref={container} tabIndex={-1} className="member-confirm" aria-label={title}>
    <strong>{title}</strong><p>{description}</p><div>
      <Button variant="outline" disabled={busy} onClick={onCancel}>취소</Button>
      <Button variant="destructive" disabled={busy} onClick={onConfirm}>{busy && <LoaderCircle className="source-spinner" size={15} />}{label}</Button>
    </div>
  </fieldset>;
}

export default function MemberManagement(props: Props) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, []);
  const labels = useMemo(() => Object.fromEntries(props.targets.map(target => [target.id, target.label])), [props.targets]);
  const admin = props.mode === 'members' && canManageMembers(props.account);
  return <section className={`member-management${admin ? '' : ' member-management--account'}`} aria-labelledby="member-management-title">
    <div className="member-page-heading"><div><h1 ref={heading} tabIndex={-1} id="member-management-title">{admin ? '회원 관리' : '내 계정'}</h1>
      <p>{admin ? '회원의 이용 상태와 저장된 채널 연결을 관리하세요.' : '채널을 연결해 두고, 다음 방송을 간편하게 준비하세요.'}</p></div>
      <Button variant="outline" onClick={props.onClose}><ArrowLeft size={16} />스튜디오로 돌아가기</Button></div>
    {admin ? <Members identity={props.identity} ownId={props.account.profile?.id || props.account.tenant_id}
      labels={labels} onSessionRevoked={props.onSessionRevoked} onOwnConnectionDeleted={props.onOwnConnectionDeleted} onOwnConnectionRemovalStarted={props.onOwnConnectionRemovalStarted} /> : <OwnAccount {...props} labels={labels} />}
  </section>;
}

function OwnAccount(props: Props & { labels: Record<string, string> }) {
  const [editing, setEditing] = useState<{ target?: string }>();
  const editorTrigger = useRef<HTMLButtonElement | null>(null);
  const connectionsHeading = useRef<HTMLHeadingElement>(null);
  const [removing, setRemoving] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const current = () => alive.current && props.identity === sessionIdentity();
  function openEditor(trigger: HTMLButtonElement, target?: string) {
    if (!current() || props.disabled || !props.canEditConnections || busy) return;
    editorTrigger.current = trigger; setRemoving(''); setError(''); setNotice(''); setEditing({ target });
  }
  function closeEditor(savedTarget?: string) {
    if (!current()) return;
    setEditing(undefined);
    if (savedTarget) setNotice(`${props.labels[savedTarget] || savedTarget} 연결을 저장했습니다. 다음 방송을 준비할 때 사용할 수 있어요.`);
    requestAnimationFrame(() => {
      if (!current()) return;
      if (editorTrigger.current?.isConnected) editorTrigger.current.focus(); else connectionsHeading.current?.focus();
    });
  }
  async function remove() {
    if (!current() || !removing || busy || props.disabled) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await props.onRemoveConnection(removing);
      if (current()) { setRemoving(''); setNotice('저장한 연결을 삭제했습니다. 방송 설정에 이미 입력한 값은 유지됩니다.'); }
    } catch (error) { if (current()) setError((error as Error).message); }
    finally { if (current()) setBusy(false); }
  }
  return <div className="member-account-layout">
    <section className="member-profile member-account-summary" aria-labelledby="account-profile-title">
      <span className="member-profile-icon"><UserRound size={22} aria-hidden="true" /></span>
      <div className="member-account-person"><h2 id="account-profile-title">로그인 계정</h2>
        <strong className="member-email">{props.account.profile?.email || '현재 로그인한 계정'}</strong>
        <span className="member-role"><ShieldCheck size={13} aria-hidden="true" />{roleName(props.account.roles)}</span></div>
      <dl className="member-account-meta"><div><dt>이용 상태</dt><dd><span className={`member-state ${props.account.profile?.enabled === false ? 'disabled' : 'enabled'}`}>{props.account.profile?.enabled === false ? '이용 정지' : '이용 중'}</span></dd></div>
        {props.account.profile && <div><dt>가입일</dt><dd>{when(props.account.profile.created_at)}</dd></div>}</dl>
    </section>
    <section className="member-surface member-own-connections channel-picker" aria-labelledby="own-connections-title">
      <div className="member-section-heading"><div><h2 ref={connectionsHeading} tabIndex={-1} id="own-connections-title">내 채널 연결 <span>{props.connections.length}개</span></h2><p>{editing ? '플랫폼의 연결 정보를 확인하고 저장하세요.' : '방송에 사용을 누르면 저장한 연결이 방송 설정에 적용됩니다.'}</p></div>
        {!editing && props.connections.length > 0 && <Button className="member-add-connection" disabled={busy || props.disabled || !props.canEditConnections} onClick={event => openEditor(event.currentTarget)}><Plus size={16} />채널 연결 추가</Button>}</div>
      {(error || props.connectionError) && <p className="message error" role="alert">{error || props.connectionError}</p>}
      {notice && <output className="message success"><Check size={16} />{notice}</output>}
      {!props.canEditConnections && <p className="member-subtle">이 계정은 채널 연결을 관리할 권한이 없습니다.</p>}
      {editing ? <ConnectionEditor key={editing.target || 'add'} identity={props.identity} targets={props.targets} initialTarget={editing.target}
        connections={props.connections} location={props.location} disabled={props.disabled || !props.canEditConnections}
        onLoad={props.onLoadConnection} onSave={props.onSaveConnection} onCancel={() => closeEditor()} onSaved={closeEditor} />
      : props.connections.length ? <ul className="member-owned-connections">{props.connections.map(connection => <li key={connection.target} className="member-connection-card">
        <div className="member-card-heading"><PlatformMark id={connection.target} /><strong>{props.labels[connection.target] || connection.target}</strong>
          {connection.has_stream_key === true && <span className="member-key-saved"><KeyRound size={12} aria-hidden="true" />키 저장됨</span>}</div>
        <dl className="member-card-meta"><div><dt>저장 위치</dt><dd>{props.location === 'browser' ? '이 브라우저' : '내 계정'}</dd></div>
          <div><dt>최근 저장</dt><dd>{when(connection.updated_at)}</dd></div></dl>
        <div className="member-card-actions"><Button className="member-use-connection" disabled={busy || props.disabled || !props.canEditConnections}
          onClick={() => { if (current()) { try { props.onUseConnection(connection.target); } catch (error) { setError((error as Error).message); } } }}><Radio size={14} />방송에 사용</Button>
          <Button variant="outline" disabled={busy || props.disabled || !props.canEditConnections} aria-label={`${props.labels[connection.target] || connection.target} 연결 수정`}
            onClick={event => openEditor(event.currentTarget, connection.target)}><Pencil size={14} />수정</Button>
          <Button className="member-card-delete" variant="ghost" disabled={busy || props.disabled || !props.canEditConnections} aria-label={`${props.labels[connection.target] || connection.target} 저장 연결 삭제`}
            onClick={() => { setRemoving(connection.target); setError(''); setNotice(''); }}><Trash2 size={15} /><span>삭제</span></Button></div>
      </li>)}</ul> : <div className="member-empty member-account-empty"><span className="member-empty-mark"><Plus size={28} aria-hidden="true" /></span><strong>첫 채널을 연결해 보세요</strong><p>방송할 플랫폼을 고르고 스트림 키를 저장하세요.<br />다음 방송에는 저장한 연결을 불러올 수 있어요.</p>
        <Button className="member-add-connection" disabled={busy || props.disabled || !props.canEditConnections} onClick={event => openEditor(event.currentTarget)}><Plus size={16} />채널 연결 추가</Button></div>}
      {!editing && <p className="member-account-storage"><ShieldCheck size={16} aria-hidden="true" /><span>{props.location === 'browser'
        ? '연결 정보는 이 브라우저에 암호화해 보관합니다. 다른 브라우저에는 동기화되지 않습니다.'
        : '연결 정보는 내 계정에 암호화해 보관합니다. 스트림 키를 변경했다면 연결도 수정해 주세요.'}</span></p>}
      {removing && <ConfirmAction title={`${props.labels[removing] || removing} 연결을 삭제할까요?`}
        description="다음에 이 채널을 선택할 때 스트림 키를 다시 입력해야 합니다. 이미 등록한 방송과 현재 방송 설정은 유지됩니다."
        label="연결 삭제" busy={busy || props.disabled} onCancel={() => setRemoving('')} onConfirm={() => void remove()} />}
    </section>
  </div>;
}

function Members({ identity, ownId, labels, onSessionRevoked, onOwnConnectionDeleted, onOwnConnectionRemovalStarted }: {
  identity: string; ownId: string; labels: Record<string, string>; onSessionRevoked: () => void; onOwnConnectionDeleted: (target: string) => void; onOwnConnectionRemovalStarted: (target: string) => void;
}) {
  const client = useMemo(() => createMemberManagementClient(identity), [identity]);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<MemberPage>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const changed = useCallback(() => { setLoading(true); setError(''); setPage(undefined); setRevision(value => value + 1); }, []);
  useEffect(() => {
    const controller = new AbortController();
    void client.list(query, offset, controller.signal).then(value => {
      if (!controller.signal.aborted && identity === sessionIdentity()) setPage(value);
    }).catch(error => { if (!controller.signal.aborted && identity === sessionIdentity()) setError((error as Error).message); })
      .finally(() => { if (!controller.signal.aborted && identity === sessionIdentity()) setLoading(false); });
    return () => controller.abort();
  }, [client, query, offset, revision, identity]);
  return <div className="member-admin-layout"><section className="member-directory member-surface" aria-labelledby="member-directory-title">
    <div className="member-section-heading"><h2 id="member-directory-title">회원 목록{page && <>{' '}<span>{page.total}명</span></>}</h2>
      <Button variant="ghost" disabled={loading} aria-label="회원 목록 새로고침" onClick={changed}><RefreshCw size={16} /></Button></div>
    <form className="member-search" onSubmit={event => { event.preventDefault(); setSelected(''); setOffset(0); setQuery(search.trim()); changed(); }}>
      <label className="sr-only" htmlFor="member-search">로그인 이메일 검색</label><Input id="member-search" value={search} maxLength={254}
        onChange={event => setSearch(event.target.value)} placeholder="로그인 이메일 검색" type="search" />
      <Button type="submit" variant="outline" aria-label="회원 검색"><Search size={17} /></Button></form>
    {error && <p className="message error" role="alert">{error}<Button variant="ghost" onClick={changed}>다시 불러오기</Button></p>}
    {loading ? <output className="member-loading"><LoaderCircle size={19} className="source-spinner" />회원을 불러오는 중…</output>
      : page?.items.length ? <ul className="member-directory-list">{page.items.map(member => <li key={member.id}><button type="button" aria-pressed={selected === member.id}
        onClick={() => setSelected(member.id)}><span className="member-avatar"><UserRound size={18} /></span><span className="member-directory-person"><strong>{member.email || '이메일 정보 없음'}</strong>
          <small>{roleName(member.roles)}{member.id === ownId ? ' · 내 계정' : ''}</small><small>저장 채널 {member.stream_connection_count}개</small></span>
        <span className={`member-state ${member.enabled ? 'enabled' : 'disabled'}`}>{member.enabled ? '이용 중' : '정지'}</span></button></li>)}</ul>
      : !error && <div className="member-empty"><Users size={26} /><strong>{query ? '검색 결과가 없습니다' : '등록된 회원이 없습니다'}</strong><p>{query ? '이메일을 다시 확인하거나 검색어를 지워 주세요.' : '회원이 로그인하면 목록에 표시됩니다.'}</p></div>}
    {page && page.total > page.limit && <div className="member-pagination"><Button variant="ghost" disabled={loading || offset === 0} onClick={() => { setOffset(Math.max(0, offset - page.limit)); changed(); }}><ChevronLeft size={16} />이전</Button>
      <span>{offset + 1}–{Math.min(offset + page.limit, page.total)} / {page.total}</span><Button variant="ghost" disabled={loading || offset + page.limit >= page.total} onClick={() => { setOffset(offset + page.limit); changed(); }}>다음<ChevronRight size={16} /></Button></div>}
  </section>{selected ? <MemberDetails key={selected} id={selected} ownId={ownId} identity={identity} client={client} labels={labels} onChanged={changed} onSessionRevoked={onSessionRevoked} onOwnConnectionDeleted={onOwnConnectionDeleted} onOwnConnectionRemovalStarted={onOwnConnectionRemovalStarted} />
    : <section className="member-surface member-detail-empty"><Users size={32} /><h2>확인할 회원을 선택하세요</h2><p>이용 상태, 로그인 세션, 저장한 플랫폼을 확인할 수 있어요.</p></section>}</div>;
}

function MemberDetails({ id, ownId, identity, client, labels, onChanged, onSessionRevoked, onOwnConnectionDeleted, onOwnConnectionRemovalStarted }: {
  id: string; ownId: string; identity: string; client: MemberManagementClient; labels: Record<string, string>; onChanged: () => void; onSessionRevoked: () => void; onOwnConnectionDeleted: (target: string) => void; onOwnConnectionRemovalStarted: (target: string) => void;
}) {
  const [member, setMember] = useState<MemberDetail>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [confirmation, setConfirmation] = useState<{ kind: 'status' | 'sessions' | 'connection'; target?: string }>();
  const lifetime = useRef<AbortController | null>(null);
  const current = () => !!lifetime.current && !lifetime.current.signal.aborted && identity === sessionIdentity();
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller;
    return () => { controller.abort(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController(); let active = true;
    void client.detail(id, controller.signal).then(value => { if (active && !controller.signal.aborted && identity === sessionIdentity()) setMember(value); })
      .catch(error => { if (active && !controller.signal.aborted && identity === sessionIdentity()) setError((error as Error).message); })
      .finally(() => { if (active && !controller.signal.aborted && identity === sessionIdentity()) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [client, id, revision, identity]);
  async function mutate() {
    if (!member || !confirmation || busy || !current()) return;
    const controller = lifetime.current!;
    setBusy(true); setError(''); setNotice('');
    try {
      let message: string;
      if (confirmation.kind === 'status') {
        const updated = await client.setEnabled(id, !member.enabled, controller.signal);
        if (!current()) return; setMember(updated);
        message = updated.enabled ? '회원이 다시 로그인할 수 있습니다.' : '이용을 정지하고 로그인 세션을 종료했습니다. 진행 중인 방송에는 중지를 요청했습니다.';
      } else if (confirmation.kind === 'sessions') {
        await client.revokeSessions(id, controller.signal);
        if (!current()) return;
        if (id === ownId) { onSessionRevoked(); return; }
        message = '회원의 모든 로그인 세션을 종료했습니다.';
      } else {
        if (id === ownId) onOwnConnectionRemovalStarted(confirmation.target!);
        await client.removeConnection(id, confirmation.target!, controller.signal);
        if (!current()) return;
        if (id === ownId) onOwnConnectionDeleted(confirmation.target!);
        message = '회원의 저장된 채널 연결을 삭제했습니다.';
      }
      if (current()) { setConfirmation(undefined); setNotice(message); setLoading(true); setRevision(value => value + 1); onChanged(); }
    } catch (error) { if (current()) setError((error as Error).message); }
    finally { if (current()) setBusy(false); }
  }
  if (!member) return <section className="member-surface member-detail-empty" aria-live="polite">{loading ? <><LoaderCircle size={24} className="source-spinner" /><p>회원 정보를 불러오는 중…</p></>
    : <><p className="message error" role="alert">{error || '회원 정보를 불러오지 못했습니다.'}</p><Button variant="outline" onClick={() => { setLoading(true); setError(''); setRevision(value => value + 1); }}>다시 불러오기</Button></>}</section>;
  const disabled = busy || loading;
  const confirmTitle = confirmation?.kind === 'status' ? member.enabled ? '이 회원의 이용을 정지할까요?' : '이 회원의 이용을 다시 허용할까요?'
    : confirmation?.kind === 'sessions' ? '모든 로그인 세션을 종료할까요?' : `${labels[confirmation?.target || ''] || confirmation?.target || ''} 저장 연결을 삭제할까요?`;
  const confirmDescription = confirmation?.kind === 'status' ? member.enabled
    ? '즉시 로그인과 API 이용을 차단하고 예약을 취소합니다. 진행 중인 방송은 중지 요청 후 종료되므로 시간이 걸릴 수 있습니다.'
    : '회원이 다시 로그인해 방송을 준비할 수 있습니다. 취소된 방송은 다시 시작되지 않습니다.'
    : confirmation?.kind === 'sessions' ? id === ownId ? '현재 접속을 포함한 모든 기기에서 로그아웃됩니다. 예약과 진행 중인 방송은 유지됩니다.'
    : '모든 기기에서 다시 로그인해야 합니다. 예약과 진행 중인 방송은 유지됩니다.'
    : '다음 방송을 준비할 때 회원이 연결 정보를 다시 입력해야 합니다. 이미 등록한 방송과 기기에 불러온 값은 유지됩니다.';
  return <section className="member-surface member-detail" aria-labelledby="member-detail-title">
    <div className="member-detail-heading"><span className="member-profile-icon"><UserRound size={23} /></span><div><h2 id="member-detail-title">{member.email || '이메일 정보 없음'}</h2><p>{roleName(member.roles)}{id === ownId ? ' · 내 계정' : ''}</p></div>
      <span className={`member-state ${member.enabled ? 'enabled' : 'disabled'}`}>{member.enabled ? '이용 중' : '이용 정지'}</span></div>
    {error && <p className="message error" role="alert">{error}</p>}{notice && <output className="message success"><Check size={16} />{notice}</output>}
    <dl className="member-facts"><div><dt>가입일</dt><dd>{when(member.created_at)}</dd></div><div><dt>최근 변경</dt><dd>{when(member.updated_at)}</dd></div>
      <div><dt>로그인 세션</dt><dd>{member.active_session_count}개</dd></div><div><dt>예약·진행 중인 작업</dt><dd>{member.active_jobs_count}개</dd></div></dl>
    <div className="member-management-actions"><Button variant="outline" disabled={disabled || !member.active_session_count} onClick={() => { setConfirmation({ kind: 'sessions' }); setNotice(''); }}><LogOut size={16} />모든 기기 로그아웃</Button>
      <Button variant={member.enabled ? 'destructive' : 'outline'} disabled={disabled || id === ownId} onClick={() => { setConfirmation({ kind: 'status' }); setNotice(''); }}>{member.enabled ? '이용 정지' : '이용 재개'}</Button></div>
    {id === ownId && <p className="member-subtle">현재 관리자는 자신의 이용을 정지할 수 없습니다.</p>}
    {confirmation && <ConfirmAction title={confirmTitle} description={confirmDescription} label={confirmation.kind === 'connection' ? '연결 삭제' : confirmation.kind === 'sessions' ? '모든 기기 로그아웃' : member.enabled ? '이용 정지' : '이용 재개'} busy={disabled} onCancel={() => setConfirmation(undefined)} onConfirm={() => void mutate()} />}
    <div className="member-detail-connections"><h3>저장된 플랫폼 <span>{member.connections.length}</span></h3><p className="member-subtle">키 내용은 표시되지 않습니다. 플랫폼과 저장 여부만 확인할 수 있어요.</p>
      {member.connections.length ? <ul className="member-connections">{member.connections.map(connection => <li key={connection.target}>
        <span className="member-channel-icon"><Radio size={17} /></span><div><strong>{labels[connection.target] || connection.target}</strong><p>스트림 키 저장됨</p><small>최근 저장 {when(connection.updated_at)}</small></div>
        <Button variant="ghost" disabled={disabled} aria-label={`${labels[connection.target] || connection.target} 회원 저장 연결 삭제`} onClick={() => { setConfirmation({ kind: 'connection', target: connection.target }); setNotice(''); }}><Trash2 size={16} />삭제</Button>
      </li>)}</ul> : <p className="member-empty-inline">저장한 플랫폼 연결이 없습니다.</p>}
    </div>
  </section>;
}
