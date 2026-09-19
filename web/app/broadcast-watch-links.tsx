'use client';

import { useState } from 'react';
import { ExternalLink, Link as LinkIcon, Save, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { normalizeWatchUrl, watchUrlError } from '@/lib/watch-links';

export type BroadcastLinks = { channel_url?: string; broadcast_url?: string };
type Props = {
  job: BroadcastLinks & { id: string; target: string; title: string };
  canEdit: boolean;
  disabled: boolean;
  onSave: (links: Required<BroadcastLinks>) => Promise<void>;
};

export function broadcastWatchLink(target: string, links: BroadcastLinks) {
  if (target === 'local') return undefined;
  for (const kind of ['broadcast', 'channel'] as const) {
    try {
      const url = normalizeWatchUrl(target, links[`${kind}_url`] || '', kind);
      if (url) return { url, kind, label: kind === 'broadcast' ? '방송 보기' : '채널 보기' };
    } catch { /* Untrusted or obsolete history URLs never become links. */ }
  }
  return undefined;
}

export default function BroadcastWatchLinks({ job, canEdit, disabled, onSave }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ channel_url: '', broadcast_url: '' });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  if (job.target === 'local') return null;
  const link = broadcastWatchLink(job.target, job);
  const channelError = watchUrlError(job.target, draft.channel_url, 'channel');
  const broadcastError = watchUrlError(job.target, draft.broadcast_url, 'broadcast');
  const locked = disabled || saving;
  async function save(event: React.SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    event.preventDefault();
    if (!canEdit || locked || channelError || broadcastError) return;
    setSaving(true); setError('');
    try {
      await onSave({ channel_url: normalizeWatchUrl(job.target, draft.channel_url, 'channel'),
        broadcast_url: normalizeWatchUrl(job.target, draft.broadcast_url, 'broadcast') });
      setEditing(false);
    } catch { setError('방송 링크를 저장하지 못했습니다. 주소를 확인하고 다시 시도하세요.'); }
    finally { setSaving(false); }
  }
  return <div className="studio-job-watch">
    <div className="studio-watch-actions">
      {link && <a href={link.url} target="_blank" rel="noopener noreferrer">{link.label}<ExternalLink size={13} aria-hidden="true" /></a>}
      {canEdit && !editing && <button type="button" disabled={disabled} onClick={() => {
        setDraft({ channel_url: job.channel_url || '', broadcast_url: job.broadcast_url || '' }); setError(''); setEditing(true);
      }} aria-label={`${job.title} 방송 링크 ${link ? '수정' : '등록'}`}><LinkIcon size={13} aria-hidden="true" />방송 링크 {link ? '수정' : '등록'}</button>}
      {link?.kind === 'channel' && <small>현재 채널 화면입니다. 이 방송의 녹화본과 다를 수 있습니다.</small>}
    </div>
    {canEdit && editing && <form className="studio-watch-editor" onSubmit={save} noValidate>
      <label htmlFor={`history-channel-${job.id}`}>시청 채널 주소 <span>선택</span></label>
      <Input id={`history-channel-${job.id}`} type="url" value={draft.channel_url} disabled={locked} autoComplete="off"
        placeholder="https://…" maxLength={2048} aria-invalid={!!channelError}
        onChange={event => setDraft(current => ({ ...current, channel_url: event.target.value }))} />
      {channelError && <p className="failure">{channelError}</p>}
      <label htmlFor={`history-broadcast-${job.id}`}>이번 방송 URL <span>선택</span></label>
      <Input id={`history-broadcast-${job.id}`} type="url" value={draft.broadcast_url} disabled={locked} autoComplete="off"
        placeholder="https://…" maxLength={2048} aria-invalid={!!broadcastError}
        onChange={event => setDraft(current => ({ ...current, broadcast_url: event.target.value }))} />
      {broadcastError && <p className="failure">{broadcastError}</p>}
      <p className="studio-watch-help">플랫폼에서 이 방송이나 녹화본의 시청 주소를 복사해 등록하세요. 비우고 저장하면 링크가 삭제됩니다. 저장된 플랫폼 연결은 변경되지 않습니다.</p>
      <div className="studio-watch-actions"><button type="submit" disabled={locked || !!channelError || !!broadcastError}><Save size={13} aria-hidden="true" />{saving ? '저장 중…' : '링크 저장'}</button>
        <button type="button" disabled={saving} onClick={() => setEditing(false)}><X size={13} aria-hidden="true" />취소</button></div>
      {error && <p className="failure" role="alert">{error}</p>}
    </form>}
  </div>;
}
