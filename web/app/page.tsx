'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { Progress } from '@/components/ui/progress';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Radio, Upload, Film, Play, Square, Clock3, Download, MonitorPlay } from 'lucide-react';

type Media = { id: string; name: string; bytes: number; duration: number; width: number; height: number; fps: number };
type Job = { id: string; title: string; target: string; media_id: string; media_name: string; state: string; duration: number; progress: number; scheduled: number; attempt: number; max_attempts: number; error: string | null };
type Event = { at: number; message: string };
const states: Record<string, string> = { scheduled: '예약 대기', starting: '연결 중', streaming: '송출 중', stopping: '중지 중', stopped: '중지됨', completed: '완료', retry_wait: '재시도 대기', failed: '실패' };
const terminal = new Set(['stopped', 'completed', 'failed']);
const duration = (s: number) => `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(Math.floor(s) % 60).padStart(2, '0')}`;
const date = (s: number) => new Date(s * 1000).toLocaleString('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set('X-Replay-Client', '1');
  const response = await fetch(`/api${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new Error(typeof body.detail === 'string' ? body.detail : `요청 실패 (${response.status})`);
  }
  return response.json();
}

export default function Home() {
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
  const refresh = useCallback(async () => {
    const [m, j, health] = await Promise.all([api<Media[]>('/media'), api<Job[]>('/broadcasts'), api<{ ffmpeg: boolean; ffprobe: boolean }>('/health')]);
    setMedia(m); setJobs(j); setConnected(true); setReady(health.ffmpeg && health.ffprobe);
    setMediaId(current => current || m[0]?.id || '');
  }, []);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { await refresh(); } catch { if (alive) setConnected(false); }
      if (alive) timer = setTimeout(poll, 1500);
    }
    void poll();
    return () => { alive = false; clearTimeout(timer); };
  }, [refresh]);
  const selectedId = selected || jobs[0]?.id;
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (!selectedId) return;
      try { const result = await api<Event[]>(`/broadcasts/${selectedId}/events`); if (alive) setEvents(result); }
      catch { if (alive) setEvents([]); }
      if (alive) timer = setTimeout(poll, 1500);
    }
    void poll();
    return () => { alive = false; clearTimeout(timer); };
  }, [selectedId]);

  async function upload(file?: File) {
    if (!file) return;
    if (file.size > 500 * 1024 * 1024) { setError('파일은 최대 500MB까지 업로드할 수 있습니다.'); return; }
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
      const job = await api<Job>('/broadcasts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ media_id: mediaId, title, target, stream_key: key, scheduled_at: schedule ? new Date(schedule).toISOString() : null }) });
      setKey(''); setConfirmed(false); setSelected(job.id); await refresh();
      setNotice(schedule ? '방송을 예약했습니다. 예약 시간까지 서버를 켜두세요.' : '방송을 송출 대기열에 등록했습니다.');
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

  const video = media.find(m => m.id === mediaId);
  const active = jobs.find(j => ['starting', 'streaming', 'stopping'].includes(j.state));
  const detail = jobs.find(j => j.id === selectedId);
  const queued = jobs.filter(j => ['scheduled', 'retry_wait'].includes(j.state)).length;
  return (
    <main className="studio">
      <header className="topbar"><div className="brand"><Radio size={25} /><strong>Replay Live</strong><span>POC</span></div><div className="connection"><i className={connected && ready ? 'online' : ''} />{connected ? ready ? '송출 엔진 준비됨' : 'FFmpeg 설치 필요' : '서버 연결 확인 중'}</div></header>
      <div className="page-title"><div><h1>녹화 영상 송출</h1><p>준비된 영상을, 원하는 시간에 라이브로.</p></div><span className="capacity">동시 송출 1개 · 대기 {queued}개</span></div>
      {!connected && <output className="message error">서버에 연결할 수 없습니다. scripts/dev.sh 실행 상태를 확인하세요.</output>}
      {error && <p role="alert" className="message error">{error}</p>}
      {notice && <output className="message success">{notice}</output>}
      <div className="workspace">
        <section className="source-pane" aria-labelledby="source-title">
          <div className="section-title"><h2 id="source-title">송출할 영상</h2><span>MP4</span></div>
          <div className="preview">
            {/* Uploaded MP4 previews do not have a supplied caption track in this POC. */}
            {/* oxlint-disable-next-line jsx-a11y/media-has-caption */}
            {video ? <video key={video.id} controls preload="metadata" src={`/api/media/${video.id}/preview`} aria-label="선택 영상 미리보기" /> : <div className="preview-empty"><Film size={48} strokeWidth={1} /><p>영상을 업로드해 시작하세요</p><span>녹화본을 처음부터 실시간 속도로 재생합니다.</span></div>}
            <span className="preview-label">소스 미리보기</span>
          </div>
          <div className="source-info"><div><strong>{video?.name || '선택된 영상 없음'}</strong><p>{video ? `${video.width} × ${video.height} / ${video.fps.toFixed(0)}fps / ${(video.bytes / 1024 / 1024).toFixed(1)}MB` : 'H.264 + AAC / 최대 1080p · 500MB'}</p></div><b className="timecode">{duration(video?.duration || 0)}</b></div>
          <div className="library-row"><Select value={mediaId || null} onValueChange={v => setMediaId(v || '')}><SelectTrigger aria-label="업로드한 영상 선택" className="media-select"><SelectValue placeholder="업로드한 영상 선택">{video?.name || '업로드한 영상 선택'}</SelectValue></SelectTrigger><SelectContent>{media.map(m => <SelectItem key={m.id} value={m.id}>{m.name}</SelectItem>)}</SelectContent></Select><Button variant="outline" disabled={!!busy} onClick={() => fileInput.current?.click()}><Upload size={16} />{busy === 'upload' ? '검사 중…' : '영상 업로드'}</Button><input ref={fileInput} type="file" accept="video/mp4,.mp4" hidden onChange={e => void upload(e.target.files?.[0])} /></div>
          <div className="on-air"><div className="section-title"><h2>현재 송출</h2><span className={`status ${active?.state || ''}`}>{active ? states[active.state] : '대기 중'}</span></div><h3>{active?.title || '아직 진행 중인 방송이 없습니다'}</h3><Progress value={active ? Math.min(100, active.progress / active.duration * 100) : 0} aria-label="현재 방송 진행률" /><div className="progress-caption"><span>{active ? duration(active.progress) : '00:00:00'}</span><span>{active ? duration(active.duration) : '방송을 등록하면 진행 상태가 표시됩니다'}</span></div>{active?.target === 'youtube' && <p className="hint">YouTube 공개 상태는 YouTube Studio에서 확인하세요.</p>}</div>
        </section>
        <section className="settings-pane" aria-labelledby="settings-title"><h2 id="settings-title">방송 설정</h2><form onSubmit={create}>
          <label htmlFor="broadcast-title">방송 이름</label><Input id="broadcast-title" placeholder="예: 신제품 소개 다시보기" value={title} onChange={e => setTitle(e.target.value)} required maxLength={120} /><p className="hint">관리용 이름입니다. YouTube 제목은 Studio에서 설정하세요.</p>
          <label htmlFor="target">송출 대상</label><Select value={target} onValueChange={v => { setTarget(v || 'local'); setConfirmed(false); }}><SelectTrigger id="target" className="full-select"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="local"><MonitorPlay size={16} /> 로컬 테스트</SelectItem><SelectItem value="youtube"><Radio size={16} /> YouTube Live</SelectItem></SelectContent></Select>
          {target === 'youtube' ? <div className="target-note"><label htmlFor="stream-key">YouTube 스트림 키</label><Input id="stream-key" type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} required placeholder="YouTube Studio의 스트림 키" /><p className="hint">라이브를 활성화하고 자동 시작/종료 설정을 확인하세요. 키는 암호화하여 저장합니다.</p><label className="check" htmlFor="confirm-broadcast"><Checkbox id="confirm-broadcast" checked={confirmed} onCheckedChange={v => setConfirmed(v === true)} /><span>이 영상의 YouTube 송출을 확인했습니다.</span></label></div> : <p className="target-note hint">외부 플랫폼에 공개하지 않고 실제 송출 결과를 FLV 파일로 저장합니다.</p>}
          <label htmlFor="scheduled">시작 시간 <span className="optional">선택</span></label><Input id="scheduled" type="datetime-local" value={schedule} onChange={e => setSchedule(e.target.value)} /><p className="hint">비워두면 바로 시작합니다. 브라우저 현지 시간 기준이며, 서버가 켜져 있어야 합니다.</p>
          <div className="schedule-summary"><Clock3 size={18} /><span>{schedule ? `${schedule.replace('T', ' ')} 시작 예정` : '등록 후 바로 송출'}<small>전체 영상 1회 재생 · 실패 시 최대 2회 재시도</small></span></div>
          <Button className="start-button" type="submit" disabled={!!busy || !video || !connected || !ready || (target === 'youtube' && !confirmed)}><Play size={16} />{busy === 'create' ? '등록 중…' : schedule ? '방송 예약' : target === 'youtube' ? 'YouTube 송출 시작' : '테스트 송출 시작'}</Button>
        </form></section>
      </div>
      <section className="history" aria-labelledby="history-title"><div className="section-title"><h2 id="history-title">방송 이력</h2><span>최근 {jobs.length}개</span></div>
      <Table><TableHeader><TableRow><TableHead>방송</TableHead><TableHead>대상 / 시작 시간</TableHead><TableHead>상태</TableHead><TableHead>진행</TableHead><TableHead className="actions-head">관리</TableHead></TableRow></TableHeader><TableBody>{jobs.length ? jobs.map(job => <TableRow key={job.id} className={selectedId === job.id ? 'selected-row' : ''}><TableCell><button className="job-title" onClick={() => { setSelected(job.id); setEvents([]); }}>{job.title}</button><small className="file-name">{job.media_name}</small></TableCell><TableCell>{job.target === 'local' ? '로컬 테스트' : 'YouTube Live'}<small>{date(job.scheduled)}</small></TableCell><TableCell><span className={`status ${job.state}`}>{states[job.state]}</span><small>시도 {job.attempt}/{job.max_attempts}</small></TableCell><TableCell className="timecode">{duration(job.progress)}<small>/ {duration(job.duration)}</small></TableCell><TableCell className="row-actions">{!terminal.has(job.state) ? <Button variant="outline" size="sm" disabled={!!busy || job.state === 'stopping'} onClick={() => void stop(job.id)}><Square size={12} />{job.state === 'scheduled' || job.state === 'retry_wait' ? '예약 취소' : '중지'}</Button> : job.state === 'completed' && job.target === 'local' ? <a className="download" href={`/api/broadcasts/${job.id}/output`}><Download size={15} /> 결과</a> : '—'}</TableCell></TableRow>) : <TableRow><TableCell colSpan={5} className="empty-history">첫 방송을 등록하면 예약과 송출 이력이 여기에 표시됩니다.</TableCell></TableRow>}</TableBody></Table>
      </section>
      {detail && <section className="event-log"><div><h2>실행 기록</h2><p>{detail.title}</p>{detail.error && <p className="failure">{detail.error}</p>}</div><ol>{events.map((event, index) => <li key={`${event.at}-${index}`}><time>{new Date(event.at * 1000).toLocaleTimeString('ko-KR', { hour12: false })}</time><span>{event.message}</span></li>)}</ol></section>}
      <footer>Replay Live POC <span>녹화본 1회 송출 · 단일 작업자 · 로컬 저장</span></footer>
    </main>
  );
}
