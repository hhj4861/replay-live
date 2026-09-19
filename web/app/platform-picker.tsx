'use client';

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Camera, Check, ChevronDown, ExternalLink, Eye, EyeOff, MonitorPlay, Music2, Play, Plus, Radio, Zap, Save, FolderOpen, Trash2, LoaderCircle, KeyRound, Pencil } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { validateStreamConnection, type StreamConnectionLocation, type StreamConnectionMetadata } from '@/lib/stream-connections';
import type { ConnectionLoadState } from '@/lib/connection-autofill';
import { normalizeWatchUrl, watchUrlError } from '@/lib/watch-links';
import './platform-picker.css';

export type StreamTarget = {
  id: string;
  label: string;
  default_server_url: string | null;
  requires_server_url: boolean;
  requires_stream_key: boolean;
  note: string;
  setup_url: string | null;
};
export type Destination = { server_url: string; stream_key: string; channel_url?: string; broadcast_url?: string };
export function broadcastDestination(target: StreamTarget, destination?: Destination) {
  const channelUrl = normalizeWatchUrl(target.id, destination?.channel_url || '', 'channel');
  const broadcastUrl = normalizeWatchUrl(target.id, destination?.broadcast_url || '', 'broadcast');
  // Keep legacy request bytes (and their retry digest) when no links were added.
  return { target: target.id, server_url: destination?.server_url || target.default_server_url || '',
    stream_key: destination?.stream_key || '', ...(channelUrl ? { channel_url: channelUrl } : {}),
    ...(broadcastUrl ? { broadcast_url: broadcastUrl } : {}) };
}
export type SavedConnectionControls = {
  location: StreamConnectionLocation;
  ready: boolean;
  saved: Record<string, StreamConnectionMetadata>;
  loads: Record<string, ConnectionLoadState>;
  error: string;
  onSave: (target: string) => void;
  onUse: (target: string) => void;
  onRemove: (target: string) => void;
};

type PlatformPickerProps = {
  catalog?: { targets: StreamTarget[]; max_destinations: number };
  targets: string[];
  destinations: Record<string, Destination>;
  onToggle: (id: string) => void;
  onDestinationChange: (id: string, value: Destination) => void;
  disabled?: boolean;
  maxDestinations: number;
  localDetails?: ReactNode;
  savedConnections?: SavedConnectionControls;
};

export function destinationReady(target: StreamTarget, destination?: Destination): boolean {
  if (watchUrlError(target.id, destination?.channel_url || '', 'channel') || watchUrlError(target.id, destination?.broadcast_url || '', 'broadcast')) return false;
  if (target.requires_stream_key) {
    try {
      validateStreamConnection({ server_url: destination?.server_url ?? target.default_server_url ?? '', stream_key: destination?.stream_key ?? '' });
      return true;
    } catch { return false; }
  }
  if (!target.requires_server_url && !target.default_server_url) return true;
  try {
    const url = new URL((destination?.server_url ?? target.default_server_url ?? '').trim());
    return ['rtmp:', 'rtmps:'].includes(url.protocol) && !!url.hostname && !url.username && !url.password && !url.hash;
  } catch {
    return false;
  }
}

export function PlatformMark({ id }: { id: string }) {
  const marks: Record<string, ReactNode> = {
    youtube: <Play size={17} fill="currentColor" strokeWidth={0} />,
    twitch: <span className="channel-mark-type channel-mark-twitch-type">tw</span>,
    facebook: <span className="channel-mark-type channel-mark-facebook-type">f</span>,
    instagram: <Camera size={19} strokeWidth={1.8} />,
    tiktok: <Music2 size={19} strokeWidth={2.5} />,
    naver: <span className="channel-mark-type">N</span>,
    chzzk: <Zap size={19} fill="currentColor" strokeWidth={1.2} />,
    kick: <span className="channel-mark-type">K</span>,
    custom: <Radio size={19} />,
    local: <MonitorPlay size={20} strokeWidth={1.8} />,
  };
  return <span className={`channel-mark channel-mark-${id}`} aria-hidden="true">{marks[id] ?? <Radio size={19} />}</span>;
}

export function ConnectionFields({ target, destination, onChange, disabled, storage, purpose = 'broadcast' }: {
  target: StreamTarget;
  destination?: Destination;
  onChange: (value: Destination) => void;
  disabled: boolean;
  storage?: SavedConnectionControls;
  purpose?: 'broadcast' | 'connection';
}) {
  const [keyVisibility, setKeyVisibility] = useState({ visible: false, version: 0 });
  const [serverSettingsOpen, setServerSettingsOpen] = useState(false);
  const [watchSettingsOpen, setWatchSettingsOpen] = useState(false);
  const [editingVersion, setEditingVersion] = useState<number | null>(null);
  const firstField = useRef<HTMLInputElement>(null);
  const focusRequested = useRef(false);
  // The account editor owns its load/save controls and always shows its fields.
  const broadcastStorage = purpose === 'broadcast' ? storage : undefined;
  const load = broadcastStorage?.loads[target.id];
  const loadedVersion = load?.appliedVersion ?? 0;
  const hasSavedKey = broadcastStorage?.saved[target.id]?.has_stream_key === true;
  const showKey = keyVisibility.visible && keyVisibility.version === loadedVersion;
  const serverUrl = destination?.server_url ?? target.default_server_url ?? '';
  const streamKey = destination?.stream_key ?? '';
  const channelUrl = destination?.channel_url ?? '';
  const broadcastUrl = destination?.broadcast_url ?? '';
  const channelError = watchUrlError(target.id, channelUrl, 'channel');
  const broadcastError = watchUrlError(target.id, broadcastUrl, 'broadcast');
  const ready = destinationReady(target, destination);
  const compact = purpose === 'broadcast' && !!broadcastStorage?.ready && hasSavedKey && loadedVersion > 0
    && ready && !load?.loading && !load?.error && !broadcastStorage?.error && editingVersion !== loadedVersion;
  const fieldsId = `connection-fields-${target.id}`;
  useEffect(() => {
    if (!compact && focusRequested.current) {
      focusRequested.current = false;
      firstField.current?.focus();
    }
  }, [compact]);
  const change = (fields: Partial<Destination>) => {
    // A valid manual draft must stay open, even after load/error metadata changes.
    if (purpose === 'broadcast') setEditingVersion(loadedVersion);
    onChange({ ...destination, server_url: serverUrl, stream_key: streamKey, ...fields });
  };
  // Keep the revised help text when web and API are updated separately.
  const connectionNote = target.id === 'instagram'
    ? 'Live Producer 이용 권한과 현재 방송의 서버 URL·키가 필요합니다.'
    : target.note;
  let serverInvalid = false;
  if (target.requires_server_url || target.default_server_url) {
    try {
      if (target.requires_server_url && !serverUrl.trim()) serverInvalid = true;
      validateStreamConnection({ server_url: serverUrl, stream_key: 'syntax-check-only' });
    } catch { serverInvalid = true; }
  }
  const serverInput = <div className="channel-field">
    <label htmlFor={`server-${target.id}`}>송출 서버 URL</label>
    <Input id={`server-${target.id}`} ref={target.requires_stream_key ? undefined : firstField} aria-label={`${target.label} 송출 서버 URL`} type="text"
      autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="rtmps://…"
      required={target.requires_server_url} value={serverUrl} maxLength={2048} disabled={disabled} aria-invalid={(serverInvalid && !!serverUrl.trim()) || undefined}
      onChange={event => change({ server_url: event.target.value })} />
    <p className="channel-field-hint">플랫폼 방송 설정에 표시된 rtmp:// 또는 rtmps:// 주소입니다.</p>
  </div>;

  return <fieldset className={`channel-connection channel-connection--${purpose}${compact ? ' is-compact' : ''}`} id={`destination-${target.id}`} disabled={disabled} aria-busy={load?.loading || undefined}>
    <legend className="sr-only">{target.label} {purpose === 'connection' ? '연결 정보' : '송출 설정'}</legend>
    <div className="channel-connection-heading">
      <div className="channel-connection-name"><PlatformMark id={target.id} /><span>{target.label}</span></div>
      <span className={`channel-entry-status${load?.loading ? ' is-loading' : ready ? ' is-complete' : ''}`} aria-live="polite">
        {load?.loading ? <LoaderCircle size={12} className="source-spinner" aria-hidden="true" /> : ready && <Check size={12} aria-hidden="true" />}
        {load?.loading ? '연결 불러오는 중' : ready ? '입력 완료' : '입력 필요'}
      </span>
    </div>
    {compact && <div className="channel-applied-summary">
      <KeyRound size={18} aria-hidden="true" />
      <div className="channel-applied-copy"><strong>저장한 연결 적용됨</strong>
        <p>{broadcastStorage?.location === 'browser' ? '이 브라우저에' : '내 계정에'} 저장한 서버 주소와 키를 입력했어요.</p>
      </div>
      <button type="button" className="channel-edit-connection" disabled={disabled} aria-label={`${target.label} 연결 정보 수정`}
        aria-expanded={false} aria-controls={fieldsId} onClick={() => {
          focusRequested.current = true;
          setKeyVisibility({ visible: false, version: loadedVersion });
          setEditingVersion(loadedVersion);
        }}><Pencil size={13} aria-hidden="true" />정보 수정</button>
    </div>}
    <div className="channel-connection-fields" id={fieldsId} hidden={compact}>{!compact && <>
      {target.requires_stream_key && <div className="channel-field">
        <label htmlFor={`key-${target.id}`}>스트림 키</label>
        <div className="channel-secret-input">
          <Input id={`key-${target.id}`} ref={firstField} aria-label={`${target.label} 스트림 키`} type={showKey ? 'text' : 'password'}
            autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="플랫폼에서 복사한 스트림 키를 붙여넣으세요"
            required maxLength={1024} value={streamKey} disabled={disabled}
            onChange={event => change({ stream_key: event.target.value })} />
          <button type="button" className="channel-secret-toggle" aria-label={`${target.label} 스트림 키 ${showKey ? '숨기기' : '보기'}`}
            aria-pressed={showKey} onClick={() => setKeyVisibility({ visible: !showKey, version: loadedVersion })} disabled={disabled}>
            {showKey ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}
          </button>
        </div>
      </div>}
      {(target.requires_server_url || target.default_server_url) && (target.default_server_url ? <details className="channel-server-details"
        open={serverSettingsOpen || !serverUrl.trim() || serverInvalid} onToggle={event => setServerSettingsOpen(event.currentTarget.open)}>
        <summary><ChevronDown size={15} aria-hidden="true" /><span>{serverUrl === target.default_server_url ? '기본 서버 주소 사용 중' : '변경한 서버 주소 사용 중'}</span><span className="channel-server-edit">확인·변경</span></summary>
        {serverInput}
      </details> : serverInput)}
      <details className="channel-watch-details" open={watchSettingsOpen || !!channelError || !!broadcastError} onToggle={event => setWatchSettingsOpen(event.currentTarget.open)}>
        <summary><ChevronDown size={15} aria-hidden="true" /><span>시청 링크</span><small>선택</small>{(channelError || broadcastError) && <span className="failure">주소 확인 필요</span>}</summary>
        <div className="channel-field"><label htmlFor={`channel-url-${target.id}`}>시청 채널 주소</label>
          <Input id={`channel-url-${target.id}`} aria-label={`${target.label} 시청 채널 주소`} type="url" value={channelUrl} maxLength={2048}
            disabled={disabled} autoComplete="off" placeholder="https://…" aria-invalid={!!channelError} onChange={event => change({ channel_url: event.target.value })} />
          <p className="channel-field-hint">시청자가 방문하는 채널의 HTTPS 주소입니다. 연결 저장 시 함께 보관합니다.</p>
          {channelError && <p className="channel-storage-error">{channelError}</p>}
        </div>
        {purpose === 'broadcast' && <div className="channel-field"><label htmlFor={`broadcast-url-${target.id}`}>이번 방송 URL</label>
          <Input id={`broadcast-url-${target.id}`} aria-label={`${target.label} 이번 방송 URL`} type="url" value={broadcastUrl} maxLength={2048}
            disabled={disabled} autoComplete="off" placeholder="https://…" aria-invalid={!!broadcastError} onChange={event => change({ broadcast_url: event.target.value })} />
          <p className="channel-field-hint">이번 방송만의 시청 주소가 있으면 입력하세요. 방송 이력에서 나중에 등록할 수 있으며 연결에는 저장하지 않습니다.</p>
          {broadcastError && <p className="channel-storage-error">{broadcastError}</p>}
        </div>}
      </details>
      {broadcastStorage && <div className="channel-saved-connection">
        <p className="channel-storage-location">{hasSavedKey && <KeyRound size={12} aria-hidden="true" />}
          {broadcastStorage.location === 'browser' ? '이 브라우저에 저장' : '내 계정에 저장'}{hasSavedKey ? '됨' : '할 수 있어요'}
        </p>
        <div className="channel-storage-actions">
          <button type="button" disabled={disabled || !broadcastStorage.ready || !ready || load?.loading} aria-label={`${target.label} 연결 저장`} onClick={() => broadcastStorage.onSave(target.id)}><Save size={13} aria-hidden="true" />연결 저장</button>
          {broadcastStorage.saved[target.id] && <>
            <button type="button" disabled={disabled || !broadcastStorage.ready || load?.loading} aria-label={`${target.label} 저장한 연결 불러오기`} onClick={() => { setKeyVisibility({ visible: false, version: loadedVersion }); broadcastStorage.onUse(target.id); }}><FolderOpen size={13} aria-hidden="true" />{load?.loading ? '불러오는 중…' : load?.error ? '다시 불러오기' : '저장한 연결 불러오기'}</button>
            <button type="button" className="channel-storage-delete" disabled={disabled || !broadcastStorage.ready} aria-label={`${target.label} 저장 삭제`} onClick={() => broadcastStorage.onRemove(target.id)}><Trash2 size={13} aria-hidden="true" />저장 삭제</button>
          </>}
        </div>
        <p className="channel-storage-help">{broadcastStorage.location === 'browser' ? '서버 주소와 키를 이 브라우저에 암호화해 보관합니다. 브라우저 데이터를 지우면 삭제됩니다.' : '서버 주소와 키를 내 계정에 암호화해 보관합니다. 다음 방송부터 자동으로 입력됩니다.'}</p>
      </div>}
    </>}</div>
    {broadcastStorage && (load?.error || broadcastStorage.error) && <div className="channel-connection-errors">
      {load?.error && <output className="channel-storage-error" role="alert">{load.error}</output>}
      {broadcastStorage.error && <output className="channel-storage-error" role="alert">{broadcastStorage.error}</output>}
    </div>}
    <div className="channel-connection-help">
      {connectionNote && <p>{connectionNote}</p>}
      {target.setup_url && <a href={target.setup_url} target="_blank" rel="noopener noreferrer">{target.label} 설정 안내<ExternalLink size={12} aria-hidden="true" /></a>}
    </div>
  </fieldset>;
}

export default function PlatformPicker({ catalog, targets, destinations, onToggle, onDestinationChange, disabled = false, maxDestinations, localDetails, savedConnections }: PlatformPickerProps) {
  const hintId = useId();
  const choices = catalog?.targets ?? [];
  const liveChoices = choices.filter(target => target.id !== 'local');
  const local = choices.find(target => target.id === 'local');
  const limitReached = targets.length >= maxDestinations;
  const selectedLive = liveChoices.filter(target => targets.includes(target.id));
  const toggle = (id: string) => {
    if (!disabled && (targets.includes(id) || !limitReached)) onToggle(id);
  };

  return <section className="channel-picker" aria-label="송출 플랫폼 선택">
    <div className="channel-picker-heading">
      <div><p id={hintId}>{limitReached ? `최대 ${maxDestinations}개를 선택했습니다. 바꾸려면 선택한 플랫폼을 해제하세요.` : maxDestinations === 1 ? '테스트할 플랫폼을 하나 선택하세요.' : '플랫폼을 눌러 추가하세요. 여러 개를 함께 선택할 수 있어요.'}</p></div>
      <span className="channel-selection-count" aria-live="polite"><strong>{targets.length}</strong><span>/ {maxDestinations} 선택</span></span>
    </div>
    {!catalog && <output className="channel-loading">플랫폼 목록을 불러오고 있어요.</output>}
    <div className="channel-grid">
      {liveChoices.map(target => {
        const selected = targets.includes(target.id);
        const unavailable = !selected && limitReached;
        const hasSavedKey = savedConnections?.saved[target.id]?.has_stream_key === true;
        const savedHintId = `${hintId}-${target.id}-saved`;
        return <button type="button" id={`platform-${target.id}`} key={target.id} className={`channel-option${selected ? ' is-selected' : ''}`}
          aria-label={target.label} aria-pressed={selected} aria-disabled={disabled || unavailable} aria-describedby={hasSavedKey ? `${hintId} ${savedHintId}` : hintId}
          disabled={disabled} onClick={() => toggle(target.id)}>
          <PlatformMark id={target.id} />
          <span className="channel-option-copy"><span className="channel-option-name">{target.label}</span>
            <span className="channel-option-action">{selected ? '선택됨' : '추가'}</span>
            {hasSavedKey && <span className="channel-key-saved" id={savedHintId}><KeyRound size={11} aria-hidden="true" />키 저장됨</span>}
          </span>
          <span className="channel-option-indicator" aria-hidden="true">{selected ? <Check size={13} strokeWidth={3} /> : <Plus size={14} />}</span>
        </button>;
      })}
    </div>
    {local && <div className={`channel-test${targets.includes('local') ? ' is-selected' : ''}`}>
      <button type="button" id="platform-local" className="channel-test-option" aria-label="파일 테스트" aria-pressed={targets.includes('local')}
        aria-disabled={disabled || (!targets.includes('local') && limitReached)} aria-describedby={hintId} disabled={disabled} onClick={() => toggle('local')}>
        <PlatformMark id="local" />
        <span className="channel-test-copy"><span>파일로 먼저 테스트</span><span>방송 전에 결과를 파일로 확인해 보세요.</span></span>
        <span className="channel-test-state">{targets.includes('local') ? <><Check size={13} aria-hidden="true" />선택됨</> : <><Plus size={13} aria-hidden="true" />추가</>}</span>
      </button>
      {targets.includes('local') && <div className="channel-test-details">{localDetails ?? <p>실제 라이브 방송 없이 결과 파일을 만듭니다.</p>}</div>}
    </div>}
    {selectedLive.length > 0 && <div className="channel-connections">
      <div className="channel-connections-heading"><h4>선택한 플랫폼 설정</h4><p>저장한 연결은 자동으로 적용됩니다. 처음 사용하는 플랫폼만 연결 정보를 입력하세요.</p></div>
      {selectedLive.map(target => <ConnectionFields key={target.id} target={target} destination={destinations[target.id]}
        onChange={value => onDestinationChange(target.id, value)} disabled={disabled} storage={savedConnections} />)}
    </div>}
  </section>;
}
