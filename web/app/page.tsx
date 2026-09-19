'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api, request, PUBLIC_TEST, CLOUD_TEST, hasSession, saveSession, sandboxExpiresAt, sessionIdentity, broadcastIdempotency, acknowledgeBroadcast, resumableSession, type LoginSession } from '@/lib/api';
import { COMMERCIAL } from '@/lib/auth';
import CommercialHome from './commercial';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Progress } from '@/components/ui/progress';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Radio, Upload, Film, Play, Square, Clock3, Download, MonitorPlay, ChevronDown } from 'lucide-react';

type Media = { id: string; name: string; bytes: number; duration: number; width: number; height: number; fps: number };
type Job = { id: string; title: string; target: string; media_id: string; media_name: string; state: string; duration: number; progress: number; scheduled: number; attempt: number; max_attempts: number; error: string | null };
type Event = { at: number; message: string };
const states: Record<string, string> = { scheduled: '예약 대기', starting: '연결 중', streaming: '송출 중', stopping: '중지 중', stopped: '중지됨', completed: '완료', retry_wait: '재시도 대기', failed: '실패' };
const terminal = new Set(['stopped', 'completed', 'failed']);
const duration = (s: number) => `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(Math.floor(s) % 60).padStart(2, '0')}`;
const date = (s: number) => new Date(s * 1000).toLocaleString('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const localInputDate = (s: number) => new Date(s * 1000 - new Date(s * 1000).getTimezoneOffset() * 60000).toISOString().slice(0, 16);

export default function Home() { return COMMERCIAL ? <CommercialHome /> : <PocHome />; }

function PocHome() {
  const [authenticated, setAuthenticated] = useState(hasSession);
  const [sandboxEnd, setSandboxEnd] = useState(() => CLOUD_TEST ? sandboxExpiresAt() : 0);
  const [now, setNow] = useState(Date.now);
  const [inviteCode, setInviteCode] = useState('');
  const [preview, setPreview] = useState<{ id: string; url: string } | null>(null);
  const maxUploadMb = PUBLIC_TEST ? 50 : 500;
  const [media, setMedia] = useState<Media[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [mediaId, setMediaId] = useState('');
  const [title, setTitle] = useState('');
  const [target, setTarget] = useState('local');
  const [key, setKey] = useState('');
  const [schedule, setSchedule] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState(false);
  const [selected, setSelected] = useState('');
  const [events, setEvents] = useState<Event[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const identity = sessionIdentity();
    const [m, j, health] = await Promise.all([api<Media[]>('/media', { signal }), api<Job[]>('/broadcasts', { signal }), api<{ ffmpeg: boolean; ffprobe: boolean; ready?: boolean }>('/health', { signal })]);
    if (signal?.aborted || identity !== sessionIdentity()) return;
    setMedia(m); setJobs(j); setConnected(true); setReady(health.ffmpeg && health.ffprobe && health.ready !== false);
    setMediaId(current => current || m[0]?.id || '');
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    const controller = new AbortController();
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { await refresh(controller.signal); } catch { if (alive) setConnected(false); }
      if (alive) timer = setTimeout(poll, 1500);
    }
    void poll();
    return () => { alive = false; controller.abort(); clearTimeout(timer); };
  }, [refresh, authenticated]);
  const selectedId = selected || jobs[0]?.id;
  useEffect(() => {
    if (!authenticated) return;
    const controller = new AbortController();
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (!selectedId) return;
      try { const result = await api<Event[]>(`/broadcasts/${selectedId}/events`, { signal: controller.signal }); if (alive) setEvents(result); }
      catch { if (alive) setEvents([]); }
      if (alive) timer = setTimeout(poll, 1500);
    }
    void poll();
    return () => { alive = false; controller.abort(); clearTimeout(timer); };
  }, [selectedId, authenticated]);

  useEffect(() => {
    if (!CLOUD_TEST || !authenticated || !sandboxEnd) return;
    const clock = setInterval(() => setNow(Date.now()), 15000);
    const expiry = setTimeout(() => window.dispatchEvent(new CustomEvent('replay-session-expired', { detail: '클라우드 테스트 시간이 끝났습니다. 접속 코드로 다시 접속하세요.' })), Math.max(0, sandboxEnd * 1000 - Date.now()));
    return () => { clearInterval(clock); clearTimeout(expiry); };
  }, [authenticated, sandboxEnd]);

  async function upload(file?: File) {
    if (!file) return;
    if (file.size > maxUploadMb * 1024 * 1024) { setError(`파일은 최대 ${maxUploadMb}MB까지 업로드할 수 있습니다.`); return; }
    setBusy('upload'); setError(''); setNotice('');
    try {
      const form = new FormData(); form.append('file', file);
      const item = await api<Media>('/media', { method: 'POST', body: form });
      setMediaId(item.id); await refresh(); setNotice('영상 검사가 완료되었습니다. 방송을 설정하세요.');
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(''); if (fileInput.current) fileInput.current.value = ''; }
  }
  async function create(e: React.SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    e.preventDefault(); setBusy('create'); setError(''); setNotice('');
    try {
      if (CLOUD_TEST && (schedule ? new Date(schedule).getTime() : Date.now()) > (sandboxEnd - 180) * 1000) {
        throw new Error('송출을 마칠 시간을 확보하기 위해 클라우드 종료 3분 전까지만 시작할 수 있습니다.');
      }
      const body = JSON.stringify({ media_id: mediaId, title, target, stream_key: key, scheduled_at: schedule ? new Date(schedule).toISOString() : null });
      const job = await api<Job>('/broadcasts', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': await broadcastIdempotency(body) }, body });
      acknowledgeBroadcast(); setKey(''); setConfirmed(false); setSelected(job.id);
      setJobs(current => [job, ...current.filter(item => item.id !== job.id)]);
      void refresh().catch(() => setConnected(false));
      setNotice(schedule ? CLOUD_TEST ? '클라우드 테스트 시간 안에 방송을 예약했습니다.' : '방송을 예약했습니다. 예약 시간까지 서버를 켜두세요.' : '방송을 송출 대기열에 등록했습니다.');
    } catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  }
  async function stop(id: string) {
    setBusy(id); setError('');
    try { await api(`/broadcasts/${id}/stop`, { method: 'POST' }); await refresh(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  }
  useEffect(() => {
    type Context = { registerTool: (tool: { name: string; description: string; inputSchema: object; annotations: object; execute: () => Promise<unknown> }, options: { signal: AbortSignal }) => unknown };
    const context = (document as Document & { modelContext?: Context }).modelContext;
    if (!context?.registerTool) return;
    const controller = new AbortController();
    try {
      void Promise.resolve(context.registerTool({ name: 'list_broadcast_status', description: '등록된 방송의 송출 상태와 진행 시간을 조회합니다. 스트림 키를 반환하지 않습니다.', inputSchema: { type: 'object', properties: {}, additionalProperties: false }, annotations: { readOnlyHint: true, untrustedContentHint: true }, execute: () => api<Job[]>('/broadcasts') }, { signal: controller.signal })).catch(() => {});
    } catch { /* Optional browser capability. */ }
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const expired = (event: globalThis.Event) => {
      setAuthenticated(false); setConnected(false); setReady(false); setMedia([]); setJobs([]); setMediaId(''); setSelected(''); setEvents([]); setPreview(null);
      setTitle(''); setKey(''); setSchedule(''); setConfirmed(false); setNotice(''); setSandboxEnd(0);
      setError((event as CustomEvent<string>).detail || '테스트 세션이 만료되었습니다. 다시 접속하세요.');
    };
    window.addEventListener('replay-session-expired', expired);
    return () => window.removeEventListener('replay-session-expired', expired);
  }, []);
  useEffect(() => {
    if (!PUBLIC_TEST || !mediaId || !authenticated) return;
    const controller = new AbortController();
    const identity = sessionIdentity();
    let url = '';
    void request(`/media/${mediaId}/preview`, { signal: controller.signal })
      .then(response => response.blob())
      .then(blob => { if (!controller.signal.aborted && identity === sessionIdentity()) { url = URL.createObjectURL(blob); setPreview({ id: mediaId, url }); } })
      .catch(() => { if (!controller.signal.aborted && hasSession()) setError('영상 미리보기를 불러오지 못했습니다.'); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [mediaId, authenticated]);
  async function login(e: React.SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    e.preventDefault(); setBusy('login'); setError('');
    try {
      const result = await api<LoginSession>('/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: inviteCode, resume_token: resumableSession() }) });
      saveSession(result); setSandboxEnd(sandboxExpiresAt()); setNow(Date.now()); setAuthenticated(true); setInviteCode('');
    } catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  }
  async function download(id: string) {
    setBusy(id); setError('');
    try {
      const identity = sessionIdentity();
      const blob = await (await request(`/broadcasts/${id}/output`)).blob();
      if (identity !== sessionIdentity()) throw new Error('테스트 세션이 변경되었습니다. 다시 접속한 뒤 결과를 확인하세요.');
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = `replay-${id.slice(0, 8)}.flv`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  }
  if (PUBLIC_TEST && !authenticated) return <main className="test-entry">
    <Radio size={34} /><h1>Replay Live 테스트</h1>
    <p>전달받은 접속 코드로 녹화 영상의 YouTube 송출을 테스트하세요.</p>
    <form onSubmit={login}><label htmlFor="test-code">테스트 접속 코드</label><Input id="test-code" type="password" autoComplete="off" value={inviteCode} onChange={e => setInviteCode(e.target.value)} required />
      <Button type="submit" disabled={!!busy}>{busy === 'login' ? '연결 중…' : '테스트 시작'}</Button>
    </form>{error && <p className="message error" role="alert">{error}</p>}
    <p className="hint">최대 50MB · 2분 영상 / 세션당 영상 3개 · 방송 5회<br />내 영상과 방송만 표시됩니다.<br />{CLOUD_TEST ? 'Vercel 클라우드 POC는 엔진 시작 후 최대 45분 동안 실행됩니다. 접속 시 남은 테스트 시간이 표시됩니다.' : '세션은 이 탭에서 24시간 유지됩니다. 운영자의 송출 서버가 실행 중일 때 사용할 수 있습니다.'}</p>
  </main>;

  const video = media.find(m => m.id === mediaId);
  const active = jobs.find(j => ['starting', 'streaming', 'stopping'].includes(j.state));
  const detail = jobs.find(j => j.id === selectedId);
  const queued = jobs.filter(j => ['scheduled', 'retry_wait'].includes(j.state)).length;
  const cloudWindowClosed = CLOUD_TEST && now > (sandboxEnd - 180) * 1000;
  return (
    <main className="studio">
      <header className="topbar"><div className="brand"><Radio size={25} /><strong>Replay Live</strong><span>POC</span></div><div className="connection"><i className={connected && ready ? 'online' : ''} />{connected ? ready ? '송출 엔진 준비됨' : '송출 엔진 점검 필요' : '서버 연결 확인 중'}</div></header>
      <div className="page-title"><div><h1>녹화 영상 송출</h1><p>준비된 영상을, 원하는 시간에 라이브로.</p></div><span className="capacity">동시 송출 1개 · 대기 {queued}개</span></div>
      {CLOUD_TEST && <p className="message">클라우드 POC · {date(sandboxEnd)} 종료 · 약 {Math.max(0, Math.ceil((sandboxEnd * 1000 - now) / 60000))}분 남음<br />기본 15초 샘플로 테스트할 수 있습니다. 예약은 {date(sandboxEnd - 180)}까지 시작할 수 있으며, 엔진 종료 시 진행 중인 송출도 중단됩니다. 대기열이 길면 예약이 지연될 수 있습니다.</p>}
      {!connected && <output className="message error">{CLOUD_TEST ? '클라우드 송출 엔진에 연결 중입니다. 연결이 종료되면 접속 코드로 다시 접속하세요.' : PUBLIC_TEST ? '송출 서버에 연결할 수 없습니다. 운영자에게 서버 실행 상태를 확인해 주세요.' : '서버에 연결할 수 없습니다. scripts/dev.sh 실행 상태를 확인하세요.'}{PUBLIC_TEST && <button type="button" onClick={() => setAuthenticated(false)}>다시 접속</button>}</output>}
      {error && <p role="alert" className="message error">{error}</p>}
      {notice && <output className="message success">{notice}</output>}
      <div className="workspace">
        <section className="source-pane" aria-labelledby="source-title">
          <div className="section-title"><h2 id="source-title">송출할 영상</h2><span>MP4</span></div>
          <div className="preview">
            {/* Uploaded MP4 previews do not have a supplied caption track in this POC. */}
            {/* oxlint-disable-next-line jsx-a11y/media-has-caption */}
            {video ? <video key={video.id} controls preload="metadata" src={PUBLIC_TEST ? (preview?.id === video.id ? preview.url : undefined) : `/api/media/${video.id}/preview`} aria-label="선택 영상 미리보기" /> : <div className="preview-empty"><Film size={48} strokeWidth={1} /><p>영상을 업로드해 시작하세요</p><span>녹화본을 처음부터 실시간 속도로 재생합니다.</span></div>}
            <span className="preview-label">소스 미리보기</span>
          </div>
          <div className="source-info"><div><strong>{video?.name || '선택된 영상 없음'}</strong><p>{video ? `${video.width} × ${video.height} / ${video.fps.toFixed(0)}fps / ${(video.bytes / 1024 / 1024).toFixed(1)}MB` : `H.264 + AAC / 최대 1080p · ${maxUploadMb}MB${PUBLIC_TEST ? ' · 2분' : ''}`}</p></div><b className="timecode">{duration(video?.duration || 0)}</b></div>
          <div className="library-row"><Select value={mediaId || null} onValueChange={v => setMediaId(v || '')}><SelectTrigger aria-label="업로드한 영상 선택" className="media-select"><SelectValue placeholder="업로드한 영상 선택">{video?.name || '업로드한 영상 선택'}</SelectValue></SelectTrigger><SelectContent>{media.map(m => <SelectItem key={m.id} value={m.id}>{m.name}</SelectItem>)}</SelectContent></Select><Button variant="outline" disabled={!!busy} onClick={() => fileInput.current?.click()}><Upload size={16} />{busy === 'upload' ? '검사 중…' : '영상 업로드'}</Button><input ref={fileInput} type="file" accept="video/mp4,.mp4" hidden onChange={e => void upload(e.target.files?.[0])} /></div>
          <div className="on-air"><div className="section-title"><h2>현재 송출</h2><span className={`status ${active?.state || ''}`}>{active ? states[active.state] : '대기 중'}</span></div><h3>{active?.title || '아직 진행 중인 방송이 없습니다'}</h3><Progress value={active ? Math.min(100, active.progress / active.duration * 100) : 0} aria-label="현재 방송 진행률" /><div className="progress-caption"><span>{active ? duration(active.progress) : '00:00:00'}</span><span>{active ? duration(active.duration) : '방송을 등록하면 진행 상태가 표시됩니다'}</span></div>{active?.target === 'youtube' && <p className="hint">YouTube 공개 상태는 YouTube Studio에서 확인하세요.</p>}</div>
        </section>
        <section className="settings-pane" aria-labelledby="settings-title"><h2 id="settings-title">방송 설정</h2><form onSubmit={create}>
          <label htmlFor="broadcast-title">방송 이름</label><Input id="broadcast-title" placeholder="예: 신제품 소개 다시보기" value={title} onChange={e => setTitle(e.target.value)} required maxLength={120} /><p className="hint">관리용 이름입니다. YouTube 제목은 Studio에서 설정하세요.</p>
          <label htmlFor="target">송출 대상</label><Select value={target} onValueChange={v => { setTarget(v || 'local'); setConfirmed(false); }}><SelectTrigger id="target" className="full-select"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="local"><MonitorPlay size={16} /> 로컬 테스트</SelectItem><SelectItem value="youtube"><Radio size={16} /> YouTube Live</SelectItem></SelectContent></Select>
          {target === 'youtube' ? <div className="target-note"><label htmlFor="stream-key">YouTube 스트림 키</label><Input id="stream-key" type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} required placeholder="YouTube Studio의 스트림 키" /><p className="hint">라이브를 활성화하고 자동 시작/종료 설정을 확인하세요. 키는 암호화하여 저장합니다.</p>
            <Collapsible className="stream-key-guide" defaultOpen={false}>
              <CollapsibleTrigger className="stream-key-guide-trigger" type="button">
                <span>YouTube 스트림 키 가져오기</span><ChevronDown size={16} aria-hidden="true" />
              </CollapsibleTrigger>
              <CollapsibleContent className="stream-key-guide-content">
              <ol>
                <li><a href="https://studio.youtube.com/" target="_blank" rel="noopener noreferrer">YouTube Studio 열기 ↗</a> 후 방송할 채널로 로그인합니다.</li>
                <li><strong>만들기 → 라이브 스트리밍 시작</strong>을 누르고 <strong>스트림</strong>을 선택합니다. 방식 선택 화면이 나오면 스트리밍 소프트웨어(인코더)를 선택하세요.</li>
                <li>방송을 만들고 <strong>스트림 설정 → 스트림 키 → 복사</strong>를 누릅니다. 테스트 방송은 비공개로 설정하세요.</li>
                <li>복사한 키를 위 입력란에 붙여넣고 방송을 시작하거나 예약합니다.</li>
              </ol>
              <p>처음 라이브를 활성화하면 최대 24시간이 걸릴 수 있습니다. 스트림 키는 채팅이나 다른 사람에게 공유하지 마세요.</p>
              <a className="guide-reference" href="https://support.google.com/youtube/answer/2907883?hl=ko" target="_blank" rel="noopener noreferrer">YouTube 공식 안내 ↗</a>
              </CollapsibleContent>
            </Collapsible>
            <label className="check" htmlFor="confirm-broadcast"><Checkbox id="confirm-broadcast" checked={confirmed} onCheckedChange={v => setConfirmed(v === true)} /><span>이 영상의 YouTube 송출을 확인했습니다.</span></label></div> : <p className="target-note hint">외부 플랫폼에 공개하지 않고 실제 송출 결과를 FLV 파일로 저장합니다.</p>}
          <label htmlFor="scheduled">시작 시간 <span className="optional">선택</span></label><Input id="scheduled" type="datetime-local" value={schedule} max={CLOUD_TEST ? localInputDate(sandboxEnd - 180) : undefined} onChange={e => setSchedule(e.target.value)} /><p className="hint">{CLOUD_TEST ? `비워두면 바로 시작합니다. 브라우저 현지 시간 기준으로 ${date(sandboxEnd - 180)}까지 예약할 수 있습니다. 종료 전 3분은 송출을 마치는 시간입니다.` : '비워두면 바로 시작합니다. 브라우저 현지 시간 기준이며, 서버가 켜져 있어야 합니다.'}</p>
          {cloudWindowClosed && <p className="message error">클라우드 종료까지 3분 미만으로 남아 새 송출을 등록할 수 없습니다. 진행 중인 방송과 결과를 확인하세요.</p>}
          <div className="schedule-summary"><Clock3 size={18} /><span>{schedule ? `${schedule.replace('T', ' ')} 시작 예정` : '등록 후 바로 송출'}<small>전체 영상 1회 재생 · 실패 시 최대 2회 재시도</small></span></div>
          <Button className="start-button" type="submit" disabled={!!busy || !video || !connected || !ready || cloudWindowClosed || (target === 'youtube' && !confirmed)}><Play size={16} />{busy === 'create' ? '등록 중…' : schedule ? '방송 예약' : target === 'youtube' ? 'YouTube 송출 시작' : '테스트 송출 시작'}</Button>
        </form></section>
      </div>
      <section className="history" aria-labelledby="history-title"><div className="section-title"><h2 id="history-title">방송 이력</h2><span>최근 {jobs.length}개</span></div>
      <Table><TableHeader><TableRow><TableHead>방송</TableHead><TableHead>대상 / 시작 시간</TableHead><TableHead>상태</TableHead><TableHead>진행</TableHead><TableHead className="actions-head">관리</TableHead></TableRow></TableHeader><TableBody>{jobs.length ? jobs.map(job => <TableRow key={job.id} className={selectedId === job.id ? 'selected-row' : ''}><TableCell><button className="job-title" onClick={() => { setSelected(job.id); setEvents([]); }}>{job.title}</button><small className="file-name">{job.media_name}</small></TableCell><TableCell>{job.target === 'local' ? '로컬 테스트' : 'YouTube Live'}<small>{date(job.scheduled)}</small></TableCell><TableCell><span className={`status ${job.state}`}>{states[job.state]}</span><small>시도 {job.attempt}/{job.max_attempts}</small></TableCell><TableCell className="timecode">{duration(job.progress)}<small>/ {duration(job.duration)}</small></TableCell><TableCell className="row-actions">{!terminal.has(job.state) ? <Button variant="outline" size="sm" disabled={!!busy || job.state === 'stopping'} onClick={() => void stop(job.id)}><Square size={12} />{job.state === 'scheduled' || job.state === 'retry_wait' ? '예약 취소' : '중지'}</Button> : job.state === 'completed' && job.target === 'local' ? <Button className="download" variant="ghost" size="sm" disabled={!!busy} onClick={() => void download(job.id)}><Download size={15} /> 결과</Button> : '—'}</TableCell></TableRow>) : <TableRow><TableCell colSpan={5} className="empty-history">첫 방송을 등록하면 예약과 송출 이력이 여기에 표시됩니다.</TableCell></TableRow>}</TableBody></Table>
      </section>
      {detail && <section className="event-log"><div><h2>실행 기록</h2><p>{detail.title}</p>{detail.error && <p className="failure">{detail.error}</p>}</div><ol>{events.map((event, index) => <li key={`${event.at}-${index}`}><time>{new Date(event.at * 1000).toLocaleTimeString('ko-KR', { hour12: false })}</time><span>{event.message}</span></li>)}</ol></section>}
      <footer>Replay Live POC <span>{CLOUD_TEST ? 'Vercel 클라우드 테스트 · 최대 45분 실행 · 50MB / 2분 영상' : PUBLIC_TEST ? '외부 테스트 · 50MB / 2분 · 세션별 데이터 분리' : '녹화본 1회 송출 · 단일 작업자 · 로컬 저장'}</span></footer>
    </main>
  );
}
