'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, Plus } from 'lucide-react';
import { api, API_BASE } from '@/lib/api';
import { Button } from '@/components/ui/button';
import './automation.css';

type Media = { id: string; name: string; status: string };
type Run = { id: string; state: string; scheduled_for: number; error_code: string | null };
type Schedule = { id: string; name: string; enabled: boolean; active: boolean; next_run: number;
  source: string; time: string; timezone: string; weekdays: number[]; targets: string[]; history: Run[] };
type Data = { enabled: boolean; items: Schedule[]; connections?: { target: string }[];
  youtube?: { configured: boolean; connected: boolean; channel: { title: string } | null } };
const days = ['월', '화', '수', '목', '금', '토', '일'];
const platforms: Record<string, string> = { youtube: 'YouTube', twitch: 'Twitch', facebook: 'Facebook',
  instagram: 'Instagram', tiktok: 'TikTok', naver: '네이버', chzzk: '치지직', kick: 'Kick', custom: '직접 등록' };
const states: Record<string, string> = { checking: '새 영상 확인 중', selected: '가져오기 준비 중', importing: '영상 가져오는 중',
  ready: '송출 준비 중', broadcasting: '송출 중', completed: '전송 완료', skipped: '새 영상 없음', failed: '실행 실패' };
const failures: Record<string, string> = {
  YOUTUBE_RECONNECT_REQUIRED: 'YouTube 채널을 다시 연결해 주세요.', YOUTUBE_ACCESS_DENIED: '채널 조회 권한을 확인해 주세요.',
  YOUTUBE_UNAVAILABLE: 'YouTube에 연결하지 못했습니다.', CONNECTION_REQUIRED: '플랫폼 연결 정보를 다시 저장해 주세요.',
  STORAGE_OR_JOB_QUOTA: '보관함 여유 공간과 진행 중인 방송을 확인해 주세요.',
  BROADCAST_FAILED: '방송 이력에서 실패한 플랫폼을 확인해 주세요.', IMPORT_FAILED: '원본 영상을 가져오지 못했습니다.',
};

export default function AutomationPanel({ media, onBack, onManageConnections, onPrepareMedia }: {
  media: Media[]; onBack: () => void; onManageConnections: () => void; onPrepareMedia: () => void;
}) {
  const [data, setData] = useState<Data | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [name, setName] = useState('');
  const [source, setSource] = useState('media');
  const [mediaId, setMediaId] = useState('');
  const [targets, setTargets] = useState<string[]>([]);
  const [weekdays, setWeekdays] = useState([0, 1, 2, 3, 4, 5, 6]);
  const [time, setTime] = useState('18:00');
  const [zone, setZone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Seoul');
  const alive = useRef(true);
  const heading = useRef<HTMLHeadingElement>(null);
  const popup = useRef<Window | null>(null);
  const refreshVersion = useRef(0);
  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current;
    const result = await api<Data>('/automations', { signal: AbortSignal.timeout(12000) });
    if (alive.current && version === refreshVersion.current) setData(result);
  }, []);
  useEffect(() => {
    alive.current = true;
    heading.current?.focus();
    void refresh().catch(() => { if (alive.current) setError('자동 송출 일정을 불러오지 못했습니다. 다시 열어 주세요.'); });
    return () => { alive.current = false; popup.current?.close(); };
  }, [refresh]);
  useEffect(() => {
    const timer = setInterval(() => { void refresh().catch(() => {}); }, 15000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    function connected(event: MessageEvent) {
      if (event.source !== popup.current || event.origin !== new URL(API_BASE, location.href).origin
          || event.data?.type !== 'replay-youtube-connected') return;
      if (event.data.ok === false) setError(event.data.message || '채널 연결을 완료하지 못했습니다. 다시 연결해 주세요.');
      else setError('');
      void refresh().catch(() => { if (alive.current) setError('채널 연결 결과를 불러오지 못했습니다.'); });
    }
    window.addEventListener('message', connected);
    return () => window.removeEventListener('message', connected);
  }, [refresh]);
  async function action(work: () => Promise<unknown>) {
    setBusy(true); setError(''); setNotice('');
    try { await work(); await refresh(); }
    catch (e) { if (alive.current) setError(e instanceof Error ? e.message : '요청을 처리하지 못했습니다.'); }
    finally { if (alive.current) setBusy(false); }
  }
  function connect() {
    popup.current = window.open('about:blank', 'replay-youtube-channel', 'width=520,height=720');
    if (!popup.current) { setError('브라우저 팝업을 허용한 뒤 다시 연결해 주세요.'); return; }
    void action(async () => {
      try {
        const result = await api<{ url: string }>('/youtube-channel/connect', { method: 'POST' });
        if (popup.current && alive.current) popup.current.location.href = result.url;
      } catch (e) { popup.current?.close(); throw e; }
    });
  }
  const ready = media.filter(item => item.status === 'ready');
  const toggle = <T,>(values: T[], value: T) => values.includes(value) ? values.filter(item => item !== value) : [...values, value];
  return <section className="automation-page" aria-labelledby="automation-title">
    <div className="automation-page-heading"><div><h1 id="automation-title" ref={heading} tabIndex={-1}>자동 송출</h1>
      <p>영상과 일정을 한 번 정하면, 정해진 시간에 방송해요.</p></div>
      <Button variant="outline" onClick={onBack}><ArrowLeft size={16} />스튜디오로 돌아가기</Button></div>
    <div className="automation-panel"><div id="automation-content" className="automation-content">
      {error && <p role="alert" className="automation-error">{error}</p>}
      {notice && <output>{notice}</output>}
      {!data ? <p>일정을 불러오는 중…</p> : !data.enabled ? <p>자동 송출을 사용할 수 없는 환경입니다.</p> : <>
        <div className="automation-toolbar"><span className="automation-count">{data.items.filter(item => item.enabled).length}개 사용 중</span><details><summary aria-label="자동 송출 도움말">? 도움말</summary><p>화면을 닫아도 실행됩니다. 로컬 확인 중에는 API와 작업 실행기를 켜 두세요. 일시중지는 다음 회차부터 적용됩니다. 플랫폼에 따라 라이브 공개 설정을 별도로 완료해야 합니다.</p></details>
          {!creating && <Button variant="outline" onClick={() => setCreating(true)}><Plus size={15} />일정 만들기</Button>}</div>
        {creating && <form className="automation-form" onSubmit={event => { event.preventDefault(); void action(async () => {
          await api('/automations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, source, media_id: mediaId,
            targets, weekdays, time, timezone: zone }) });
          if (alive.current) { setCreating(false); setName(''); setNotice('자동 송출 일정을 저장했습니다.'); }
        }); }}>
          <label>일정 이름<input required maxLength={180} value={name} onChange={event => setName(event.target.value)} placeholder="예: 매주 신제품 소개" /></label>
          <fieldset className="automation-source"><legend>방송할 영상</legend>
            <label><input type="radio" name="automation-source" checked={source === 'media'} onChange={() => setSource('media')} />보관함 영상 반복</label>
            <label><input type="radio" name="automation-source" checked={source === 'youtube_latest'} onChange={() => setSource('youtube_latest')} />내 YouTube 최신 영상</label>
          </fieldset>
          {source === 'media' ? <label>보관함 영상<select aria-label="보관함 영상" required value={mediaId} onChange={event => setMediaId(event.target.value)}><option value="">영상 선택</option>{ready.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>{!ready.length && <small>먼저 보관함에 영상을 추가해 주세요. <button type="button" onClick={onPrepareMedia}>영상 추가하기</button></small>}</label>
            : <div className="automation-channel"><strong>{data.youtube?.channel?.title || '내 YouTube 채널'}</strong><p>새 공개 영상이 있을 때만 송출해요.</p>
              {data.youtube?.connected ? <><span>연결됨</span><button type="button" disabled={busy} onClick={() => void action(() => api('/youtube-channel', { method: 'DELETE' }))}>연결 해제</button></>
                : <Button type="button" variant="outline" disabled={busy || !data.youtube?.configured} onClick={connect}>YouTube 채널 연결</Button>}
              {!data.youtube?.configured && <p>채널 연결 설정 준비 중입니다. 보관함 영상으로 일정을 만들 수 있어요.</p>}
            </div>}
          <fieldset className="automation-days"><legend>반복 요일</legend>{days.map((day, index) => <label key={day}><input type="checkbox" checked={weekdays.includes(index)} onChange={() => setWeekdays(toggle(weekdays, index))} /><span>{day}</span></label>)}</fieldset>
          <div className="automation-time"><label>송출 시간<input type="time" required value={time} onChange={event => setTime(event.target.value)} /></label><label>시간대<input aria-label="시간대" required value={zone} onChange={event => setZone(event.target.value)} list="automation-zones" /><datalist id="automation-zones"><option value="Asia/Seoul">Asia/Seoul</option><option value="Asia/Tokyo">Asia/Tokyo</option><option value="America/Los_Angeles">America/Los_Angeles</option><option value="America/New_York">America/New_York</option><option value="Europe/London">Europe/London</option><option value="UTC">UTC</option></datalist></label></div>
          <fieldset className="automation-targets"><legend>저장한 송출 플랫폼</legend>{data.connections?.length ? data.connections.map(item => <label key={item.target}><input type="checkbox" checked={targets.includes(item.target)} onChange={() => setTargets(toggle(targets, item.target))} />{platforms[item.target] || item.target}</label>) : <p>송출할 플랫폼 연결을 먼저 저장해 주세요. <button type="button" onClick={onManageConnections}>내 계정에서 연결하기</button></p>}</fieldset>
          <div className="automation-actions"><Button type="button" variant="ghost" disabled={busy} onClick={() => setCreating(false)}>취소</Button><Button type="submit" disabled={busy || !targets.length || !weekdays.length || (source === 'media' ? !mediaId : !data.youtube?.connected)}>{busy ? '저장 중…' : '자동 송출 시작'}</Button></div>
        </form>}
        {!data.items.length && !creating && <p className="automation-empty">매일 또는 원하는 요일에 방송할 일정을 만들어 보세요.</p>}
        <div className="automation-list">{data.items.map(item => <article key={item.id} className="automation-rule">
          <div className="automation-rule-time"><strong>{item.time}</strong><span>{item.weekdays.length === 7 ? '매일' : item.weekdays.map(day => days[day]).join('·')}</span></div>
          <div className="automation-rule-main"><h3>{item.name}</h3><p>{item.targets.map(target => platforms[target] || target).join(', ')} · {item.source === 'youtube_latest' ? '새 영상만' : '보관함 반복'}</p>
            <small>{item.active ? '이번 회차 진행 중' : item.enabled ? `다음 ${new Date(item.next_run * 1000).toLocaleString('ko-KR', { timeZone: item.timezone })}` : '일시중지'} · {item.timezone}</small>
            {!!item.history.length && <details><summary>최근 결과: {states[item.history[0].state] || item.history[0].state}</summary><ol>{item.history.map(run => <li key={run.id}><time>{new Date(run.scheduled_for * 1000).toLocaleString('ko-KR', { timeZone: item.timezone })}</time> — {states[run.state] || run.state}{run.state === 'failed' && <p className="automation-error">{failures[run.error_code || ''] || '영상을 가져오거나 송출하지 못했습니다. 보관함과 방송 이력을 확인해 주세요.'}</p>}</li>)}</ol></details>}
          </div><div className="automation-rule-actions"><Button variant="outline" size="sm" disabled={busy} aria-label={`${item.name} ${item.enabled ? '일시중지' : '다시 시작'}`} onClick={() => void action(() => api(`/automations/${item.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !item.enabled }) }))}>{item.enabled ? '일시중지' : '다시 시작'}</Button>
            <button type="button" disabled={busy || item.active} aria-label={`${item.name} 삭제`} onClick={() => void action(() => api(`/automations/${item.id}`, { method: 'DELETE' }))}>삭제</button></div>
        </article>)}</div>
      </>}
    </div></div>
  </section>;
}
