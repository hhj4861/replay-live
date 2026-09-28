'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, CalendarClock, Check, CheckCircle2, ChevronDown, CircleHelp, Clock3, Library, Pause, Play, Plus, Radio, Trash2, Video } from 'lucide-react';
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
const zones: Record<string, string> = { 'Asia/Seoul': '한국 시간 (서울)', 'Asia/Tokyo': '일본 시간 (도쿄)',
  'America/Los_Angeles': '미국 서부 (로스앤젤레스)', 'America/New_York': '미국 동부 (뉴욕)',
  'Europe/London': '영국 시간 (런던)', UTC: '세계 표준시 (UTC)' };
function repeatLabel(values: number[]) {
  if (values.length === 7) return '매일';
  if (values.length === 5 && [0, 1, 2, 3, 4].every(day => values.includes(day))) return '평일';
  if (values.length === 2 && values.includes(5) && values.includes(6)) return '주말';
  return [...values].sort((a, b) => a - b).map(day => days[day]).join(' · ');
}
function dateLabel(value: number, timeZone: string) {
  return new Date(value * 1000).toLocaleString('ko-KR', { timeZone, month: 'long', day: 'numeric',
    weekday: 'short', hour: 'numeric', minute: '2-digit' });
}
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
  const nameInput = useRef<HTMLInputElement>(null);
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
  useEffect(() => { if (creating) nameInput.current?.focus(); }, [creating]);
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
  const unavailable = !targets.length ? '송출할 플랫폼을 선택해 주세요.' : !weekdays.length ? '반복할 요일을 선택해 주세요.'
    : source === 'media' ? (!mediaId ? '보관함에서 영상을 선택해 주세요.' : '')
      : !data?.youtube?.connected ? '먼저 내 YouTube 채널을 연결해 주세요.' : '';
  const activeCount = data?.items.filter(item => item.enabled).length || 0;
  const createButton = <Button className="automation-button automation-primary" disabled={busy} onClick={() => { setError(''); setNotice(''); setCreating(true); }}><Plus size={18} />일정 만들기</Button>;
  return <section className="automation-page" aria-labelledby="automation-title">
    <button type="button" className="automation-back" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />스튜디오</button>
    <div className="automation-page-heading"><div><h1 id="automation-title" ref={heading} tabIndex={-1}>자동 송출</h1>
      <p>한 번 예약하고, 원하는 시간에 꾸준히 방송하세요.</p></div>
      {data?.enabled && !!data.items.length && !creating && createButton}</div>
    <div className="automation-panel"><div id="automation-content" className="automation-content">
      {error && <p role="alert" className="automation-error">{error}</p>}
      {notice && <output className="automation-notice"><CheckCircle2 size={18} aria-hidden="true" />{notice}</output>}
      {!data ? <output className="automation-loading">일정을 불러오고 있어요…</output> : !data.enabled ? <p className="automation-loading">자동 송출을 사용할 수 없는 환경입니다.</p> : <>
        <div className="automation-toolbar"><div className="automation-count"><CalendarClock size={18} aria-hidden="true" /><strong>{creating ? '새 일정' : '내 송출 일정'}</strong>{!creating && <span>{activeCount}개 예약 중</span>}</div>
          <details className="automation-help"><summary aria-label="자동 송출 도움말"><CircleHelp size={17} aria-hidden="true" />이용 안내<ChevronDown size={15} aria-hidden="true" /></summary>
            <div><p>예약은 저장한 플랫폼 연결로 실행돼요. 플랫폼의 라이브 시작·공개 설정도 확인해 주세요.</p><p>일시중지는 다음 방송부터 적용돼요. 이미 시작한 방송은 방송 이력에서 중단할 수 있어요.</p><p>로컬 테스트 중에는 이 컴퓨터의 실행 프로그램을 켜 두세요.</p></div>
          </details></div>
        {creating && <form className="automation-form" onSubmit={event => { event.preventDefault(); void action(async () => {
          await api('/automations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, source, media_id: mediaId,
            targets, weekdays, time, timezone: zone }) });
          if (alive.current) { setCreating(false); setName(''); setNotice('자동 송출 일정을 저장했습니다.'); }
        }); }}>
          <label className="automation-name">일정 이름<input ref={nameInput} required disabled={busy} maxLength={180} value={name} onChange={event => setName(event.target.value)} placeholder="예: 평일 저녁 신제품 소개" /></label>
          <div className="automation-form-sections">
            <section className="automation-form-section automation-video" aria-labelledby="automation-video-title"><h2 id="automation-video-title"><Library size={19} aria-hidden="true" />방송할 영상</h2>
              <fieldset className="automation-source" disabled={busy}><legend className="sr-only">영상 선택 방식</legend>
                <label><input type="radio" name="automation-source" aria-label="보관함 영상 반복" checked={source === 'media'} onChange={() => setSource('media')} /><span className="automation-option-icon"><Library size={20} aria-hidden="true" /></span><span><strong>보관함 영상 반복</strong><small>선택한 영상을 매번 방송해요.</small></span><Check className="automation-option-check" size={17} aria-hidden="true" /></label>
                <label><input type="radio" name="automation-source" aria-label="내 YouTube 최신 영상" checked={source === 'youtube_latest'} onChange={() => setSource('youtube_latest')} /><span className="automation-option-icon"><Video size={20} aria-hidden="true" /></span><span><strong>내 YouTube 최신 영상</strong><small>새로 올라온 공개 영상만 방송해요.</small></span><Check className="automation-option-check" size={17} aria-hidden="true" /></label>
              </fieldset>
              {source === 'media' ? <div><label className="automation-media-select">보관함 영상<select aria-label="보관함 영상" required disabled={busy} value={mediaId} onChange={event => setMediaId(event.target.value)}><option value="">방송할 영상을 선택하세요</option>{ready.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>{!ready.length && <div className="automation-setup"><span>아직 준비된 영상이 없어요.</span><Button className="automation-button" variant="outline" type="button" onClick={onPrepareMedia}><Plus size={16} />영상 추가하기</Button></div>}</div>
                : <div className="automation-channel"><div><strong>{data.youtube?.channel?.title || '내 YouTube 채널'}</strong><p>새 공개 영상이 있을 때만 송출해요.</p></div>
                  {data.youtube?.connected ? <div className="automation-channel-actions"><span className="automation-status"><Check size={14} aria-hidden="true" />연결됨</span><Button className="automation-button automation-secondary" variant="ghost" type="button" disabled={busy} onClick={() => void action(() => api('/youtube-channel', { method: 'DELETE' }))}>연결 해제</Button></div>
                    : data.youtube?.configured ? <Button className="automation-button" type="button" variant="outline" disabled={busy} onClick={connect}><Video size={17} />YouTube 채널 연결</Button>
                      : <p className="automation-channel-unavailable">채널 연결을 준비하고 있어요. 지금은 보관함 영상으로 예약할 수 있어요.</p>}
                </div>}
            </section>
            <section className="automation-form-section" aria-labelledby="automation-time-title"><h2 id="automation-time-title"><Clock3 size={19} aria-hidden="true" />반복 시간</h2>
              <fieldset className="automation-days" disabled={busy}><legend>반복 요일<span>{repeatLabel(weekdays) || '요일을 선택하세요'}</span></legend><div>{days.map((day, index) => <label key={day}><input type="checkbox" aria-label={`${day}요일`} checked={weekdays.includes(index)} onChange={() => setWeekdays(toggle(weekdays, index))} /><span>{day}</span></label>)}</div></fieldset>
              <div className="automation-time"><label>송출 시간<input type="time" required disabled={busy} value={time} onChange={event => setTime(event.target.value)} /></label><label>시간대<select aria-label="시간대" required disabled={busy} value={zone} onChange={event => setZone(event.target.value)}>{!zones[zone] && <option value={zone}>{zone}</option>}{Object.entries(zones).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
            </section>
            <section className="automation-form-section" aria-labelledby="automation-target-title"><h2 id="automation-target-title"><Radio size={19} aria-hidden="true" />송출 플랫폼</h2>
              <fieldset className="automation-targets" disabled={busy}><legend className="sr-only">저장한 송출 플랫폼</legend>{data.connections?.length ? <><p>연결한 플랫폼을 선택하세요.</p><div>{data.connections.map(item => <label key={item.target}><input type="checkbox" aria-label={platforms[item.target] || item.target} checked={targets.includes(item.target)} onChange={() => setTargets(toggle(targets, item.target))} /><span>{platforms[item.target] || item.target}</span><Check size={16} aria-hidden="true" /></label>)}</div></> : <div className="automation-setup"><p>방송할 채널을 먼저 연결해 주세요.<br />한 번 연결하면 다음 예약에도 사용할 수 있어요.</p><Button className="automation-button" variant="outline" type="button" onClick={onManageConnections}><Plus size={16} />내 계정에서 연결하기</Button></div>}</fieldset>
            </section>
          </div>
          <div className="automation-actions"><p id="automation-submit-hint">{unavailable || `${repeatLabel(weekdays)} ${time}, 선택한 플랫폼으로 자동 송출해요.`}</p><div><Button className="automation-button automation-secondary" type="button" variant="ghost" disabled={busy} onClick={() => { setCreating(false); heading.current?.focus(); }}>취소</Button><Button className="automation-button automation-primary" type="submit" aria-describedby="automation-submit-hint" disabled={busy || !!unavailable}><CalendarClock size={17} />{busy ? '저장 중…' : '자동 송출 시작'}</Button></div></div>
        </form>}
        {!data.items.length && !creating && <div className="automation-empty"><span className="automation-empty-icon"><CalendarClock size={30} aria-hidden="true" /></span><h2>다음 방송은 미리 예약해 두세요.</h2><p>영상과 반복 시간을 정하면<br />연결한 채널로 알아서 방송해요.</p>{createButton}</div>}
        <div className="automation-list">{data.items.map(item => <article key={item.id} className="automation-rule">
          <div className="automation-rule-time"><strong>{item.time}</strong><span>{repeatLabel(item.weekdays)}</span></div>
          <div className="automation-rule-main"><div className="automation-rule-title"><h3>{item.name}</h3><span className={`automation-status ${!item.enabled && !item.active ? 'paused' : ''}`}>{item.active ? '진행 중' : item.enabled ? '예약 중' : '일시중지'}</span></div>
            <p>{item.targets.map(target => platforms[target] || target).join(', ')}<span className="automation-separator" aria-hidden="true" />{item.source === 'youtube_latest' ? '내 채널의 새 영상' : '보관함 영상 반복'}</p>
            <p className="automation-next">{item.active ? '이번 방송이 끝나면 다음 일정을 준비해요.' : item.enabled ? `다음 방송 ${dateLabel(item.next_run, item.timezone)}` : '다시 시작하면 다음 예약부터 방송해요.'}<span className="automation-zone">{zones[item.timezone] || item.timezone}</span></p>
            {!!item.history.length && <details className="automation-history"><summary><span>최근 결과: {states[item.history[0].state] || item.history[0].state}</span><ChevronDown size={16} aria-hidden="true" /></summary><ol>{item.history.map(run => <li key={run.id}><div><time>{dateLabel(run.scheduled_for, item.timezone)}</time><strong>{states[run.state] || run.state}</strong></div>{run.state === 'failed' && <p className="automation-error">{failures[run.error_code || ''] || '영상을 가져오거나 송출하지 못했습니다. 보관함과 방송 이력을 확인해 주세요.'}</p>}</li>)}</ol></details>}
          </div><div className="automation-rule-actions"><Button className="automation-button" variant="outline" disabled={busy} aria-label={`${item.name} ${item.enabled ? '일시중지' : '다시 시작'}`} onClick={() => void action(() => api(`/automations/${item.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !item.enabled }) }))}>{item.enabled ? <Pause size={15} /> : <Play size={15} />}{item.enabled ? '일시중지' : '다시 시작'}</Button>
            <Button className="automation-button automation-delete" variant="ghost" type="button" disabled={busy || item.active} aria-label={`${item.name} 삭제`} onClick={() => void action(() => api(`/automations/${item.id}`, { method: 'DELETE' }))}><Trash2 size={16} />삭제</Button></div>
        </article>)}</div>
      </>}
    </div></div>
  </section>;
}
