'use client';

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { sha256 } from '@noble/hashes/sha2.js';
import { api, broadcastIdempotency, acknowledgeBroadcast, sessionIdentity, assertSessionIdentity, revokeCurrentSession } from '@/lib/api';
import { AUTH_PROVIDER, completeSignIn, signIn, signOut } from '@/lib/auth';
import { createStreamConnectionStore, streamConnectionLocation, type StreamConnectionOwner,
  type StreamConnectionStore, type StreamConnectionMetadata, type StreamConnectionValue } from '@/lib/stream-connections';
import { createConnectionAutofill } from '@/lib/connection-autofill';
import { normalizeWatchUrl } from '@/lib/watch-links';
import GoogleSignIn from './google-sign-in';
import PlatformPicker, { broadcastDestination, destinationReady, type StreamTarget, type Destination } from './platform-picker';
import BroadcastWatchLinks, { type BroadcastLinks } from './broadcast-watch-links';
import MemberManagement from './member-management';
import { canManageMembers, type Account } from '@/lib/member-management';
import { createMediaSelection } from './media-selection';
import { createLocalImporter, LocalImportError, type LocalImportPhase } from '@/lib/local-import';
import LocalImportConnection from './local-import-connection';
import LocalImportFailure, { type ImportFailure } from './local-import-failure';
import './commercial-studio.css';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { Radio, Upload, Play, Square, Download, Trash2, ChevronDown, Link as LinkIcon, LoaderCircle,
  Check, ArrowRight, Video, Library, CalendarClock, Clock3, CircleHelp, ArrowUpRight, X, History, UserRound, Users } from 'lucide-react';

type Media = { id: string; name: string; status: string; bytes: number; duration: number; error_code?: string };
type Job = BroadcastLinks & { id: string; title: string; media_id: string; media_name?: string; target: string; state: string; progress: number; duration: number; scheduled: number; error_code?: string };
type Signed = { url: string; method: string; headers: Record<string, string> };
type Health = { ready: boolean; max_upload_mb: number; max_duration_seconds: number; retention_days: number; max_concurrent: number };
type TargetCatalog = { targets: StreamTarget[]; max_destinations: number };
type SourcePlatform = { id: string; label: string; note: string };
type SourceCatalog = { sources: SourcePlatform[] };
type Usage = { storage_bytes: number; storage_limit_bytes: number; storage_reserved_bytes: number; storage_available_bytes: number; reserved_runtime_seconds_today: number; runtime_limit_seconds_per_day: number };
type OutputEstimate = { media_id: string; estimated_output_bytes: number; max_output_bytes: number; storage_available_bytes: number; can_create: boolean; reason: string | null };
const states: Record<string, string> = { importing: '링크에서 영상 가져오는 중', pending: '영상 검사 대기', uploading: '업로드 대기', validating: '영상 검사 중', ready: '사용 가능', scheduled: '예약 대기', starting: '연결 중', streaming: '송출 중', stopping: '중지 중', stopped: '중지됨', completed: '완료', failed: '실패', retry_wait: '재시도 대기' };
const terminal = new Set(['completed', 'failed', 'stopped']);
const size = (bytes: number) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
const clock = (seconds: number) => `${Math.floor(seconds / 60)}분 ${Math.floor(seconds % 60)}초`;
const failures: Record<string, string> = {
  DEVICE_IMPORT_TASK_FAILED: '서버에서 가져오기 작업을 받지 못했습니다. 인터넷 연결을 확인하고 다시 시도하세요.',
  DEVICE_IMPORT_COMPLETE_FAILED: '업로드 완료를 서버에서 확인하지 못했습니다. 보관함 상태를 확인한 뒤 다시 시도하세요.',
  DEVICE_IMPORT_REVOKED: '가져오기 권한이 만료되었거나 취소됐습니다. 로그인과 내 컴퓨터 연결을 확인하고 다시 시도하세요.',
  DEVICE_IMPORT_UPLOAD_FAILED: '내 컴퓨터에서 보관함으로 업로드하지 못했습니다. 네트워크 연결을 확인하고 다시 시도하세요.',
  DEVICE_IMPORT_INVALID: '도우미와 서버의 작업 정보가 일치하지 않습니다. 도우미를 업데이트하고 다시 연결하세요.',
  OUTPUT_LIMIT_EXCEEDED: '결과 파일이 허용 크기를 초과했습니다. 더 짧은 영상으로 다시 시도하세요.',
  OUTPUT_STORAGE_QUOTA_EXCEEDED: '결과 파일을 위한 저장 공간이 부족합니다.',
  OUTPUT_BUDGET_MISSING: '결과 파일의 저장 공간을 확보하지 못했습니다. 다시 등록하세요.',
  SOURCE_URL_INVALID: '녹화 영상의 전체 HTTPS 링크를 확인하세요.',
  SOURCE_URL_UNSAFE: '이 주소에서는 영상을 가져올 수 없습니다. 공개된 플랫폼 링크 또는 HTTPS MP4 다운로드 링크를 입력하세요.',
  SOURCE_PROVIDER_INVALID: '원본 영상 플랫폼을 다시 선택하세요.',
  SOURCE_PROVIDER_MISMATCH: '선택한 원본 플랫폼과 링크의 사이트가 다릅니다. 플랫폼 선택을 확인하세요.',
  SOURCE_RECORDING_REQUIRED: '생방송·재생목록 대신 녹화 영상 한 편의 링크를 입력하세요.',
  SOURCE_RESTRICTED: '로그인이나 플랫폼의 접근 제한으로 가져올 수 없습니다. 접근 가능한 MP4 다운로드 링크 또는 파일 업로드를 사용하세요.',
  SOURCE_BOT_CHECK_REQUIRED: '원본 플랫폼이 봇 확인을 요구해 영상을 가져오지 못했습니다. 브라우저에서 재생되는 공개 영상도 해당될 수 있습니다. 본인 영상의 MP4 파일을 업로드하세요.',
  SOURCE_ACCESS_DENIED: '원본 플랫폼이 영상 다운로드를 허용하지 않았습니다. 본인 영상의 MP4 파일을 업로드하거나, 사용 가능한 새 MP4 다운로드 링크를 입력하세요.',
  SOURCE_RATE_LIMITED: '원본 플랫폼이 요청을 일시적으로 제한했습니다. 잠시 후 다시 시도하거나 MP4 파일을 업로드하세요.',
  SOURCE_NOT_FOUND: '원본 플랫폼에서 영상 파일을 찾지 못했습니다. 링크가 유효한지 확인하거나 MP4 파일을 업로드하세요.',
  SOURCE_UNAVAILABLE: '영상 정보를 확인하거나 파일을 내려받지 못했습니다. 공개 영상도 플랫폼의 접근 제한으로 실패할 수 있습니다. MP4 파일 업로드로 계속할 수 있습니다.',
  SOURCE_FORMAT_UNSUPPORTED: '이 영상 형식은 링크에서 가져올 수 없습니다. MP4 파일로 업로드하세요.',
  SOURCE_DURATION_EXCEEDED: '영상이 허용 재생 시간을 초과합니다. 더 짧은 녹화 영상을 선택하세요.',
  SOURCE_TOO_LARGE: '영상이 허용 크기 또는 남은 저장 공간을 초과합니다. 더 작은 영상을 선택하거나 보관함을 정리하세요.',
  SOURCE_TIMEOUT: '가져오기 시간이 초과되었습니다. 잠시 후 다시 시도하거나 MP4 파일을 업로드하세요.',
  SOURCE_DNS_UNAVAILABLE: '원본 영상 서버에 연결할 수 없습니다. 링크를 확인하고 잠시 후 다시 시도하세요.',
  SOURCE_TOO_COMPLEX: '이 영상의 다운로드 방식은 지원하지 않습니다. MP4 파일로 업로드하세요.',
  SOURCE_METADATA_TOO_LARGE: '플랫폼의 영상 정보를 처리할 수 없습니다. MP4 파일로 업로드하세요.',
  SOURCE_REDIRECT_INVALID: '영상 링크가 지원하지 않는 주소로 이동합니다. 원본 영상의 전체 링크를 입력하세요.',
  SOURCE_INCOMPLETE: '영상 다운로드가 끝나지 않았습니다. 다시 시도하거나 MP4 파일을 업로드하세요.',
  SOURCE_ENCODING_UNSUPPORTED: '이 다운로드 형식은 지원하지 않습니다. MP4 파일로 업로드하세요.',
  SOURCE_EXTRACTOR_UNAVAILABLE: '현재 이 플랫폼의 영상을 가져올 수 없습니다. MP4 파일로 업로드하세요.',
};

export default function CommercialHome() {
  const [authenticated, setAuthenticated] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [connected, setConnected] = useState(false);
  const [health, setHealth] = useState<Health>();
  const [usage, setUsage] = useState<Usage>();
  const [outputEstimate, setOutputEstimate] = useState<OutputEstimate>();
  const [media, setMedia] = useState<Media[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [roles, setRoles] = useState<string[]>([]);
  const [account, setAccount] = useState<Account>();
  const [managementView, setManagementView] = useState<'account' | 'members' | ''>('');
  const managementTrigger = useRef<HTMLButtonElement | null>(null);
  const [mediaId, setMediaId] = useState('');
  const [preview, setPreview] = useState<{ id: string; url: string } | null>(null);
  const [title, setTitle] = useState('');
  const [targets, setTargets] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<TargetCatalog>();
  const [sourceCatalog, setSourceCatalog] = useState<SourceCatalog>();
  const [sourceMode, setSourceMode] = useState<'library' | 'link' | 'file'>('link');
  const [sourceProvider, setSourceProvider] = useState('youtube');
  const [sourceUrl, setSourceUrl] = useState('');
  const [sourceName, setSourceName] = useState('');
  const [importId, setImportId] = useState('');
  const [preparationKind, setPreparationKind] = useState<'import' | 'upload'>('import');
  const [destinations, setDestinations] = useState<Record<string, Destination>>({});
  const [connectionOwner, setConnectionOwner] = useState<(StreamConnectionOwner & { identity: string })>();
  const [connectionStore, setConnectionStore] = useState<StreamConnectionStore>();
  const [savedConnections, setSavedConnections] = useState<Record<string, StreamConnectionMetadata>>({});
  const [connectionStorageError, setConnectionStorageError] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [schedule, setSchedule] = useState('');
  const [startMode, setStartMode] = useState<'now' | 'scheduled'>('now');
  const [historyFilter, setHistoryFilter] = useState<'all' | 'active' | 'finished'>('all');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [importFailure, setImportFailure] = useState<ImportFailure | null>(null);
  const [notice, setNotice] = useState('');
  const [uploadProgress, setUploadProgress] = useState(0);
  const [events, setEvents] = useState<{ at: number; code?: string; message?: string }[]>([]);
  const picker = useRef<HTMLInputElement>(null);
  const pendingUpload = useRef<{ id: string; digest: string } | null>(null);
  const [mediaSelection] = useState(() => createMediaSelection());
  const [localImporter] = useState(() => createLocalImporter());
  const [localConnected, setLocalConnected] = useState(false);
  const [localPhase, setLocalPhase] = useState<LocalImportPhase | ''>('');
  const localImportAbort = useRef<AbortController | null>(null);
  const uploading = useRef<XMLHttpRequest | null>(null);
  const actionVersion = useRef(0);
  const refreshVersion = useRef(0);
  const connectionStoreRef = useRef<StreamConnectionStore | undefined>(undefined);
  const connectionOwnerRef = useRef<(StreamConnectionOwner & { identity: string }) | undefined>(undefined);
  const connectionListVersion = useRef(0);
  const [connectionAutofill] = useState(() => createConnectionAutofill());
  useEffect(() => {
    connectionAutofill.configure({
      isCurrentStore: store => store === connectionStoreRef.current && store.identity === sessionIdentity(),
      apply: (id, value, accept) => {
        setDestinations(current => accept() ? { ...current, [id]: { ...current[id], server_url: value.server_url, stream_key: value.stream_key, channel_url: value.channel_url || '' } } : current);
        setConfirmed(false);
      },
    });
    return () => connectionAutofill.reset();
  }, [connectionAutofill]);
  const connectionLoads = useSyncExternalStore(connectionAutofill.subscribe, connectionAutofill.getSnapshot, connectionAutofill.getSnapshot);
  const canOperate = roles.includes('admin') || roles.includes('operator');
  const hasLocal = targets.includes('local');
  const hasLive = targets.some(target => target !== 'local');
  const maxDestinations = catalog?.max_destinations || health?.max_concurrent || 1;
  const tooMany = targets.length > maxDestinations;
  const importedMedia = media.find(item => item.id === importId);
  const importPending = !!importId && (!importedMedia || !['ready', 'failed', 'stopped'].includes(importedMedia.status));
  const sourcePlatform = sourceCatalog?.sources.find(item => item.id === sourceProvider);
  const googleSignedIn = useCallback(() => { setAuthenticated(true); setError(''); }, []);
  const resetAccount = useCallback(() => {
    connectionAutofill.reset();
    mediaSelection.reset();
    actionVersion.current += 1; uploading.current?.abort(); uploading.current = null; pendingUpload.current = null;
    setAuthenticated(false); setConnected(false); setMedia([]); setJobs([]); setDestinations({}); setPreview(null); setRoles([]);
    setMediaId(''); setUsage(undefined); setHealth(undefined); setCatalog(undefined); setOutputEstimate(undefined);
    setEvents([]); setConfirmed(false); setBusy(''); setError(''); setImportFailure(null); setNotice(''); setUploadProgress(0);
    setTitle(''); setTargets([]); setSchedule(''); setStartMode('now'); setHistoryFilter('all');
    localImportAbort.current?.abort(); localImportAbort.current = null;
    void localImporter.disconnect(); setLocalConnected(false); setLocalPhase('');
    setSourceCatalog(undefined); setSourceMode('link'); setSourceProvider('youtube'); setSourceUrl(''); setSourceName(''); setImportId('');
    connectionStoreRef.current = undefined; connectionOwnerRef.current = undefined; connectionListVersion.current += 1;
    setConnectionOwner(undefined); setConnectionStore(undefined); setSavedConnections({}); setConnectionStorageError('');
    setAccount(undefined); setManagementView(''); managementTrigger.current = null;
  }, [connectionAutofill, mediaSelection, localImporter]);
  useEffect(() => () => { localImportAbort.current?.abort(); void localImporter.disconnect(); }, [localImporter]);
  useEffect(() => {
    window.addEventListener('replay-signin-required', resetAccount);
    return () => window.removeEventListener('replay-signin-required', resetAccount);
  }, [resetAccount]);

  useEffect(() => { let alive = true; void completeSignIn().then(value => { if (alive) setAuthenticated(value); }).catch(() => { if (alive) setError('로그인 연결을 확인할 수 없습니다. 다시 시도하세요.'); }).finally(() => { if (alive) setInitializing(false); }); return () => { alive = false; }; }, []);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const identity = sessionIdentity();
    const version = ++refreshVersion.current;
    const selectionGeneration = mediaSelection.capture();
    const [m, j, h, u, me, choices, sources] = await Promise.all([api<Media[]>('/media', { signal }), api<Job[]>('/broadcasts', { signal }), api<Health>('/health', { signal }).catch(() => undefined), api<Usage>('/usage', { signal }), api<Account>('/me', { signal }), api<TargetCatalog>('/stream-targets', { signal }), api<SourceCatalog>('/media-sources', { signal }).catch(() => undefined)]);
    if (signal?.aborted || version !== refreshVersion.current) return;
    assertSessionIdentity(identity);
    const previousOwner = connectionOwnerRef.current;
    if (!previousOwner || previousOwner.identity !== identity || previousOwner.tenant_id !== me.tenant_id || previousOwner.subject !== me.subject) {
      const nextOwner = { tenant_id: me.tenant_id, subject: me.subject, identity };
      connectionStoreRef.current = undefined; connectionListVersion.current += 1;
      connectionOwnerRef.current = nextOwner; setConnectionOwner(nextOwner);
      setConnectionStore(undefined); setSavedConnections({}); setConnectionStorageError('');
      if (previousOwner) { setImportFailure(null); localImportAbort.current?.abort(); void localImporter.disconnect(); setLocalConnected(false); connectionAutofill.reset(); mediaSelection.reset(); setImportId(''); setMediaId(''); setPreview(null); setDestinations({}); setConfirmed(false); setManagementView(''); }
    }
    if (!me.roles.includes('admin') && !me.roles.includes('operator')) {
      connectionStoreRef.current = undefined; setConnectionStore(undefined); setSavedConnections({});
      connectionAutofill.reset(); setDestinations({}); setConfirmed(false);
    }
    setMedia(m); setJobs(j); setHealth(current => h || (current ? { ...current, ready: false } : undefined)); setUsage(u); setRoles(me.roles); setCatalog(choices); setSourceCatalog(sources); setConnected(true);
    setAccount(me);
    if (!canManageMembers(me)) setManagementView(current => current === 'members' ? '' : current);
    const selection = mediaSelection.reconcile(m, identity, selectionGeneration);
    if (selection.kind === 'ready') {
      setMediaId(current => mediaSelection.capture() === selectionGeneration ? selection.id : current); setSourceMode('library');
      setNotice('영상이 준비됐습니다. 방송할 채널을 선택해 주세요.');
    } else if (selection.kind === 'default') {
      setMediaId(current => mediaSelection.capture() !== selectionGeneration ? current : current && m.some(item => item.id === current && item.status === 'ready') ? current : selection.allowFirst ? m.find(item => item.status === 'ready')?.id || '' : '');
    }
  }, [connectionAutofill, mediaSelection, localImporter]);
  useEffect(() => {
    if (!authenticated) return;
    const controller = new AbortController(); let active = true; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { try { await refresh(controller.signal); } catch { if (active) setConnected(false); } if (active) timer = setTimeout(poll, 3000); };
    void poll(); return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [authenticated, refresh]);
  useEffect(() => {
    connectionStoreRef.current = undefined; connectionListVersion.current += 1;
    if (!authenticated || !canOperate || !connectionOwner) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function initialize() {
      try {
        const store = await createStreamConnectionStore(connectionOwner!, connectionOwner!.identity);
        if (!active) return;
        connectionStoreRef.current = store; setConnectionStore(store);
        const poll = async () => {
          const version = ++connectionListVersion.current;
          try {
            const result = await store.list();
            if (active && store === connectionStoreRef.current && version === connectionListVersion.current) {
              setSavedConnections(Object.fromEntries(result.map(value => [value.target, value]))); setConnectionStorageError('');
            }
          } catch (error) {
            if (active && store === connectionStoreRef.current && version === connectionListVersion.current) setConnectionStorageError((error as Error).message);
          }
          if (active) timer = setTimeout(poll, 30_000);
        };
        void poll();
      } catch (error) { if (active) setConnectionStorageError((error as Error).message); }
    }
    void initialize();
    return () => { active = false; clearTimeout(timer); connectionStoreRef.current = undefined; connectionListVersion.current += 1; };
  }, [authenticated, canOperate, connectionOwner]);
  useEffect(() => {
    connectionAutofill.sync(authenticated && canOperate && connectionStore === connectionStoreRef.current ? connectionStore : undefined,
      targets, Object.keys(savedConnections));
  }, [authenticated, canOperate, connectionStore, targets, savedConnections, connectionAutofill]);
  useEffect(() => {
    if (!mediaId || !authenticated) return;
    const controller = new AbortController();
    void api<Signed>(`/media/${mediaId}/preview`, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setPreview({ id: mediaId, url: value.url }); }).catch(() => {});
    return () => controller.abort();
  }, [mediaId, authenticated]);
  useEffect(() => {
    if (!authenticated || !mediaId || !hasLocal) return;
    const controller = new AbortController();
    void api<OutputEstimate>(`/media/${mediaId}/output-estimate`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setOutputEstimate(value); })
      .catch(() => { if (!controller.signal.aborted) setOutputEstimate(undefined); });
    return () => controller.abort();
  }, [authenticated, mediaId, hasLocal, usage?.storage_bytes]);

  async function action(name: string, task: () => Promise<void>) {
    const identity = sessionIdentity(); const version = ++actionVersion.current;
    setBusy(name); setError(''); setImportFailure(null); setNotice('');
    try { await task(); } catch (err) {
      if (version === actionVersion.current && identity === sessionIdentity()) {
        if ((err as Error).name === 'AbortError') setNotice('영상 가져오기를 취소했습니다.');
        else {
          const message = failures[(err as Error).message] || (err as Error).message;
          if (name === 'import' || name === 'local-connect') {
            const titles: Record<LocalImportPhase, string> = { requesting: '가져오기 요청에 실패했습니다.',
              downloading: '내 컴퓨터에서 영상을 내려받지 못했습니다.', transferring: '영상 파일을 전달하지 못했습니다.',
              uploading: '보관함 업로드에 실패했습니다.', validating: '업로드 완료 확인에 실패했습니다.' };
            setImportFailure({ title: name === 'local-connect' ? '도우미를 연결하지 못했습니다.'
              : err instanceof LocalImportError ? titles[err.phase] : '영상 가져오기를 시작하지 못했습니다.',
              message, connect: name === 'local-connect' });
          } else setError(message);
        }
      }
    }
    finally { if (version === actionVersion.current) setBusy(''); }
  }
  function currentConnectionStore() {
    const store = connectionStoreRef.current;
    if (!canOperate || !store) throw new Error('로그인 계정과 연결 저장소를 확인한 뒤 다시 시도하세요.');
    assertSessionIdentity(store.identity);
    return store;
  }
  function confirmConnectionStore(store: StreamConnectionStore) {
    assertSessionIdentity(store.identity);
    if (store !== connectionStoreRef.current) throw new Error('로그인 계정이 변경되어 연결 요청이 취소되었습니다.');
    connectionListVersion.current += 1; setConnectionStorageError('');
  }
  async function loadAccountConnection(id: string) {
    const store = currentConnectionStore();
    const value = await store.use(id);
    assertSessionIdentity(store.identity);
    if (store !== connectionStoreRef.current || value.target !== id) throw new Error('연결 정보를 다시 확인해 주세요.');
    return { server_url: value.server_url, stream_key: value.stream_key, channel_url: value.channel_url || '' };
  }
  async function saveAccountConnection(id: string, value: StreamConnectionValue) {
    const store = currentConnectionStore();
    const target = catalog?.targets.find(item => item.id === id && item.id !== 'local');
    if (!target || !destinationReady(target, value)) throw new Error('송출 서버 URL과 스트림 키를 먼저 입력하세요.');
    const saved = await store.save(id, { server_url: value.server_url, stream_key: value.stream_key,
      channel_url: normalizeWatchUrl(id, value.channel_url || '', 'channel') });
    confirmConnectionStore(store);
    setSavedConnections(current => ({ ...current, [id]: saved }));
  }
  async function saveConnection(id: string) {
    await action('connection-save', async () => {
      const store = currentConnectionStore();
      const target = catalog?.targets.find(item => item.id === id);
      if (!target || !destinationReady(target, destinations[id])) throw new Error('송출 서버 URL과 스트림 키를 먼저 입력하세요.');
      const saved = await store.save(id, { server_url: destinations[id]?.server_url ?? target.default_server_url ?? '', stream_key: destinations[id]?.stream_key ?? '',
        channel_url: normalizeWatchUrl(id, destinations[id]?.channel_url || '', 'channel') });
      confirmConnectionStore(store); setSavedConnections(current => ({ ...current, [id]: saved }));
      setNotice(`${target.label} 연결을 ${store.location === 'browser' ? '이 브라우저' : '내 계정'}에 저장했습니다.`);
    });
  }
  async function removeConnection(id: string) {
    await action('connection-remove', async () => {
      await deleteSavedConnection(id);
      setNotice('저장한 연결을 삭제했습니다. 현재 방송 설정에 입력된 값은 유지됩니다.');
    });
  }
  async function deleteSavedConnection(id: string) {
    const store = currentConnectionStore();
    connectionAutofill.remove(id);
    await store.remove(id); confirmConnectionStore(store);
    setSavedConnections(current => { const next = { ...current }; delete next[id]; return next; });
  }
  function ownConnectionRemovalStarted(id: string) {
    if (!connectionOwnerRef.current || connectionOwnerRef.current.identity !== sessionIdentity()) return;
    if (connectionStoreRef.current?.location === 'account') {
      connectionListVersion.current += 1; connectionAutofill.remove(id);
    }
  }
  function ownConnectionDeleted(id: string) {
    // The admin API already deleted this account's server record. Never remove
    // this browser's separate IndexedDB record or issue a second delete/use.
    if (!connectionOwnerRef.current || connectionOwnerRef.current.identity !== sessionIdentity()) return;
    if (connectionStoreRef.current?.location === 'account') {
      connectionListVersion.current += 1;
      setSavedConnections(current => { const next = { ...current }; delete next[id]; return next; });
    }
  }
  function closeManagement() {
    setManagementView('');
    requestAnimationFrame(() => managementTrigger.current?.focus());
  }
  function useSavedConnection(id: string) {
    currentConnectionStore();
    if (!targets.includes(id)) {
      if (targets.length >= maxDestinations) {
        throw new Error(`채널은 최대 ${maxDestinations}개까지 선택할 수 있습니다. 스튜디오에서 선택한 채널을 해제한 뒤 다시 선택하세요.`);
      }
      toggleTarget(id);
    } else connectionAutofill.retry(id);
    setConfirmed(false);
    closeManagement();
    requestAnimationFrame(() => document.getElementById('channels-title')?.scrollIntoView({ block: 'start' }));
  }
  async function sendUpload(file: File, identity: string, kind: 'upload' | 'import', signal?: AbortSignal, expectedDigest?: string) {
    const check = () => { assertSessionIdentity(identity); signal?.throwIfAborted(); };
    check();
    if (!health || !file.name.toLowerCase().endsWith('.mp4') || !file.size || file.size > health.max_upload_mb * 1024 ** 2) throw new Error('허용 크기 이내의 MP4 파일을 선택하세요.');
    const selection = mediaSelection.begin(identity);
    setMediaId(''); setPreview(null); setConfirmed(false); setImportId(''); setPreparationKind(kind);
    setUploadProgress(0);
    const hash = sha256.create();
    for (let offset = 0; offset < file.size; offset += 4 * 1024 ** 2) {
      const chunk = await file.slice(offset, offset + 4 * 1024 ** 2).arrayBuffer();
      check(); hash.update(new Uint8Array(chunk));
    }
    const digest = Array.from(hash.digest(), b => b.toString(16).padStart(2, '0')).join('');
    if (expectedDigest && digest !== expectedDigest) throw new Error('도우미에서 받은 영상의 무결성 검사에 실패했습니다.');
    let id = pendingUpload.current?.digest === digest ? pendingUpload.current.id : '';
    let uploadedBytes = !!id;
    try {
      if (!id) {
        const result = await api<{ media: Media; upload: Signed }>('/uploads', { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: file.name, bytes: file.size, sha256: digest }) });
        id = result.media.id;
        check();
        await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest(); xhr.open(result.upload.method, result.upload.url); xhr.timeout = 15 * 60_000;
          uploading.current = xhr;
          const abort = () => xhr.abort();
          signal?.addEventListener('abort', abort, { once: true });
          xhr.onloadend = () => signal?.removeEventListener('abort', abort);
          for (const [key, value] of Object.entries(result.upload.headers)) if (key.toLowerCase() !== 'content-length') xhr.setRequestHeader(key, value);
          xhr.upload.onprogress = event => { if (identity === sessionIdentity() && event.lengthComputable) setUploadProgress(event.loaded / event.total * 100); };
          xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error('업로드에 실패했습니다. 다시 시도하세요.'));
          xhr.onerror = xhr.ontimeout = () => reject(new Error('업로드 연결이 끊겼습니다. 다시 시도하세요.'));
          xhr.onabort = () => reject(new DOMException('영상 업로드가 취소되었습니다.', 'AbortError'));
          xhr.send(file);
        }).finally(() => { uploading.current = null; });
        check(); uploadedBytes = true;
        pendingUpload.current = { id, digest };
      }
      check();
      const uploaded = await api<Media>(`/uploads/${id}/complete`, { method: 'POST', signal });
      check(); pendingUpload.current = null;
      if (!mediaSelection.register(selection, id)) return;
      setImportId(id); setMedia(current => [uploaded, ...current.filter(item => item.id !== id)]);
      setNotice('보관함에 업로드했습니다. 검사가 끝나면 이 영상이 자동으로 선택됩니다.');
      void refresh().catch(() => setConnected(false));
    } catch (error) {
      if (id && !uploadedBytes && identity === sessionIdentity()) {
        await api(`/media/${id}`, { method: 'DELETE', signal: AbortSignal.timeout(5000) }).catch(() => undefined);
      }
      throw error;
    }
  }
  async function upload(file?: File) {
    if (!file || !health) return;
    await action('upload', () => sendUpload(file, sessionIdentity(), 'upload'));
    if (picker.current) picker.current.value = '';
  }
  async function connectLocal(code: string) {
    await action('local-connect', async () => {
      setLocalConnected(false);
      await localImporter.pair(code); setLocalConnected(localImporter.isConnected());
      setNotice('내 컴퓨터를 연결했습니다. 영상 링크를 입력해 주세요.');
    });
  }
  async function importSource(event?: React.SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    event?.preventDefault();
    await action('import', async () => {
      const identity = sessionIdentity();
      if (importPending || !sourcePlatform || !health) throw new Error('원본 플랫폼과 진행 중인 가져오기를 확인하세요.');
      if (!localImporter.isConnected()) { setLocalConnected(false); throw new Error('내 컴퓨터를 먼저 연결해 주세요.'); }
      let url: URL;
      try { url = new URL(sourceUrl.trim()); } catch { throw new Error('녹화 영상의 전체 HTTPS 링크를 입력하세요.'); }
      if (url.protocol !== 'https:' || (url.port && url.port !== '443') || url.username || url.password || url.hash || url.href.length > 4096) throw new Error('계정 정보나 # 조각 주소가 없는 HTTPS 영상 링크를 입력하세요.');
      const maxBytes = Math.min(health.max_upload_mb * 1024 ** 2, usage?.storage_available_bytes ?? Infinity, 50 * 1024 ** 2);
      if (maxBytes < 1) throw new Error('보관함의 저장 공간이 부족합니다.');
      const controller = new AbortController(); localImportAbort.current = controller;
      const selection = mediaSelection.begin(identity);
      setMediaId(''); setPreview(null); setImportId(''); setConfirmed(false); setPreparationKind('import');
      try {
        const uploaded = await localImporter.runCloud<Media>({ provider: sourceProvider, url: url.href, name: sourceName }, controller.signal,
          phase => { if (identity === sessionIdentity()) setLocalPhase(phase); });
        assertSessionIdentity(identity); controller.signal.throwIfAborted();
        if (!mediaSelection.register(selection, uploaded.id)) return;
        setImportId(uploaded.id); setMedia(current => [uploaded, ...current.filter(item => item.id !== uploaded.id)]);
        setNotice('내 컴퓨터에서 보관함에 업로드했습니다. 검사가 끝나면 이 영상이 자동으로 선택됩니다.');
        void refresh().catch(() => setConnected(false));
      } finally {
        if (identity === sessionIdentity()) { setLocalConnected(localImporter.isConnected()); setLocalPhase(''); }
        if (localImportAbort.current === controller) localImportAbort.current = null;
      }
    });
  }
  async function create(event: React.SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    event.preventDefault();
    await action('create', async () => {
      const identity = sessionIdentity();
      if (pendingReason) throw new Error(pendingReason);
      if (importPending || !media.some(item => item.id === mediaId && item.status === 'ready')) throw new Error('영상 가져오기와 검사가 완료된 뒤 송출하세요.');
      if (!targets.length || tooMany || !catalog) throw new Error('송출 대상 수와 동시 송출 한도를 확인하세요.');
      if (!title.trim()) throw new Error('방송 이름을 입력해 주세요.');
      if (catalog.targets.some(item => targets.includes(item.id) && !destinationReady(item, destinations[item.id]))) throw new Error('선택한 채널의 연결 정보를 입력해 주세요.');
      if (hasLive && !confirmed) throw new Error('선택한 채널의 라이브 준비를 확인해 주세요.');
      if (startMode === 'scheduled' && (!schedule || !Number.isFinite(new Date(schedule).getTime()))) throw new Error('예약 날짜와 시간을 선택해 주세요.');
      const choices = catalog.targets.filter(item => targets.includes(item.id)).map(item => broadcastDestination(item, destinations[item.id]));
      if (choices.length !== targets.length) throw new Error('송출 대상 목록을 새로 불러오세요.');
      const common = { media_id: mediaId, title: title.trim(), scheduled_at: startMode === 'scheduled' && schedule ? new Date(schedule).toISOString() : null };
      const multiple = choices.length > 1;
      const body = JSON.stringify(multiple ? { ...common, destinations: choices } : { ...common, ...choices[0] });
      const idempotency = await broadcastIdempotency(body);
      assertSessionIdentity(identity);
      const result = await api<Job | { jobs: Job[] }>(multiple ? '/broadcast-batches' : '/broadcasts', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotency }, body });
      const created = 'jobs' in result ? result.jobs : [result];
      acknowledgeBroadcast(); targets.forEach(id => connectionAutofill.edit(id)); setDestinations({}); setConfirmed(false);
      setJobs(current => [...created, ...current.filter(item => !created.some(job => job.id === item.id))]);
      setNotice(multiple ? `${created.length}개 송출 대상을 함께 등록했습니다. 각 대상의 시작 상태를 확인하세요.`
        : schedule ? '방송을 예약했습니다. 화면을 닫아도 예약은 유지됩니다.' : '방송을 등록했습니다. 시작 상태를 확인하세요.');
      void refresh().catch(() => setConnected(false));
    });
  }
  async function saveWatchLinks(job: Job, links: Required<BroadcastLinks>) {
    if (!canOperate) throw new Error('방송 링크를 수정할 권한이 없습니다.');
    const identity = sessionIdentity();
    const result = await api<Job>(`/broadcasts/${job.id}/watch-links`, { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(links), signal: AbortSignal.timeout(12_000) });
    assertSessionIdentity(identity); refreshVersion.current += 1;
    setJobs(current => current.map(item => item.id === result.id ? result : item));
  }
  const selected = media.find(item => item.id === mediaId);
  const selectedTargets = catalog?.targets.filter(item => targets.includes(item.id)) || [];
  const missingDestinations = selectedTargets.filter(item => !destinationReady(item, destinations[item.id]));
  const sourceReady = selected?.status === 'ready' && !importPending;
  const loadingConnection = targets.find(id => connectionLoads[id]?.loading);
  const channelsReady = targets.length > 0 && selectedTargets.length === targets.length && !tooMany && !loadingConnection && !missingDestinations.length;
  const scheduleReady = startMode === 'now' || (!!schedule && Number.isFinite(new Date(schedule).getTime()));
  const pendingReason = !canOperate ? '현재 계정은 조회만 할 수 있습니다.' : !connected ? '서버에 다시 연결하고 있습니다.'
    : !health?.ready ? '송출 준비 상태를 확인하고 있습니다.' : importPending ? '영상 가져오기와 검사가 끝나면 시작할 수 있어요.'
    : !sourceReady ? '방송할 영상을 먼저 준비해 주세요.' : !targets.length ? '방송할 채널을 하나 이상 선택해 주세요.'
    : tooMany ? `채널은 최대 ${maxDestinations}개까지 선택할 수 있어요.` : loadingConnection ? '저장한 채널 연결을 불러오고 있어요.' : !channelsReady ? `${missingDestinations[0]?.label || '선택한 채널'}의 연결 정보를 입력해 주세요.`
    : !title.trim() ? '방송 이름을 입력해 주세요.' : !scheduleReady ? '예약 날짜와 시간을 선택해 주세요.'
    : hasLive && !confirmed ? '선택한 채널의 라이브 준비를 확인해 주세요.' : '';
  const activeJobs = jobs.filter(job => !terminal.has(job.state));
  const visibleJobs = jobs.filter(job => historyFilter === 'all' || (historyFilter === 'active' ? !terminal.has(job.state) : terminal.has(job.state)));
  const startDescription = startMode === 'now' ? '지금 시작' : schedule ? new Date(schedule).toLocaleString('ko-KR', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '시간 선택 필요';
  function toggleTarget(id: string) {
    if (!catalog?.targets.some(item => item.id === id) || !!busy || !canOperate) return;
    if (!targets.includes(id) && targets.length >= maxDestinations) return;
    connectionAutofill.select(id, !targets.includes(id));
    setTargets(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]);
    setDestinations(current => { const next = { ...current }; delete next[id]; return next; });
    setConfirmed(false);
  }
  if (!authenticated) return <main className="studio-welcome">
    <div className="studio-welcome-brand brand"><Radio size={26} /><strong>Replay Live</strong></div>
    <div className="studio-welcome-layout"><section className="studio-welcome-intro"><span className="studio-welcome-label"><Video size={16} />크리에이터를 위한 방송 스튜디오</span>
      <h1>준비된 영상으로,<br />다시 만나는 라이브.</h1><p>녹화해 둔 영상을 여러 채널에 동시에 방송하세요.<br />영상 준비부터 예약까지, 한곳에서 간편하게.</p>
      <div className="studio-welcome-flow" aria-label="녹화 영상 하나를 여러 채널로 방송"><div className="studio-welcome-video"><span><Video size={24} />내 녹화 영상</span><span className="studio-welcome-play"><Play size={24} fill="currentColor" /></span><div className="studio-welcome-timeline"><i /></div></div><ArrowRight className="studio-welcome-arrow" /><div className="studio-welcome-channels"><span><i />YouTube Live</span><span><i />TikTok LIVE</span><span><i />여러 채널에 동시에</span></div></div>
      <div className="studio-welcome-features"><span><Check size={16} />링크·파일로 영상 준비</span><span><Check size={16} />여러 채널 동시 송출</span><span><Check size={16} />원하는 시간에 예약</span></div>
    </section><section className="studio-auth-card" aria-labelledby="login-title"><span className="studio-auth-icon"><Radio size={26} /></span><h2 id="login-title">방송 준비를 시작하세요</h2><p>로그인하고 내 영상과 방송을 관리하세요.</p>
      {AUTH_PROVIDER !== 'oidc' && !initializing && <GoogleSignIn onSuccess={googleSignedIn} developmentPreview={AUTH_PROVIDER === 'development'} />}
      {AUTH_PROVIDER !== 'google' && <Button className="organization-login" disabled={initializing || !!busy} onClick={() => void action('login', signIn)}>{initializing ? '로그인 확인 중…' : AUTH_PROVIDER === 'development' ? '개발용 미리보기로 접속' : '계정으로 로그인'}</Button>}
      {AUTH_PROVIDER === 'google' && initializing && <output className="hint">로그인 확인 중…</output>}
      {error && <p className="message error" role="alert">{error}</p>}<p className="studio-auth-note">로그아웃해도 등록한 예약 방송은 유지됩니다.</p>
    </section></div><footer className="studio-welcome-footer">Replay Live<span>영상은 준비하고, 방송은 편리하게.</span></footer>
  </main>;
  return <main className="studio replay-studio">
    <header className="topbar studio-topbar member-topbar"><a href="#studio-top" className="brand" onClick={() => setManagementView('')}><span className="studio-brand-icon"><Radio size={22} /></span><strong>Replay Live</strong></a><nav aria-label="스튜디오 메뉴"><a href="#broadcast-history" onClick={() => setManagementView('')}><History size={16} />방송 이력{activeJobs.length > 0 && <span className="studio-nav-count">{activeJobs.length}</span>}</a>
      <Button variant="ghost" className="member-nav-button" disabled={!account || !!busy} aria-pressed={managementView === 'account'} onClick={event => { managementTrigger.current = event.currentTarget; setManagementView('account'); }}><UserRound size={16} />내 계정</Button>
      {canManageMembers(account) && <Button variant="ghost" className="member-nav-button" disabled={!!busy} aria-pressed={managementView === 'members'} onClick={event => { managementTrigger.current = event.currentTarget; setManagementView('members'); }}><Users size={16} />회원 관리</Button>}
    </nav><div className="connection"><span className="studio-connection-status"><i className={connected && health?.ready ? 'online' : ''} />{connected ? health?.ready ? '스튜디오 연결됨' : '준비 상태 확인 중' : '재연결 중'}</span><Button variant="ghost" disabled={!!busy} onClick={() => {
      const revocation = revokeCurrentSession();
      resetAccount();
      const local = signOut().then(() => true, () => false);
      const identity = sessionIdentity();
      void Promise.all([revocation, local]).then(([revoked, signedOut]) => {
        if (identity === sessionIdentity() && (!revoked || !signedOut)) setError('이 기기에서는 로그아웃했습니다. 서버나 로그인 제공자의 세션 종료는 확인하지 못했습니다.');
      });
    }}>로그아웃</Button></div></header>
    {managementView && account && connectionOwner ? <MemberManagement key={`${connectionOwner.identity}:${connectionOwner.tenant_id}:${connectionOwner.subject}:${managementView}`}
      mode={managementView} identity={connectionOwner.identity} account={account} targets={catalog?.targets || []}
      connections={Object.values(savedConnections)} location={connectionStore?.location ?? streamConnectionLocation()} connectionError={connectionStorageError}
      canEditConnections={canOperate && !!connectionStore} disabled={!!busy || !connected} onClose={closeManagement}
      onLoadConnection={loadAccountConnection} onSaveConnection={saveAccountConnection}
      onUseConnection={useSavedConnection} onRemoveConnection={deleteSavedConnection} onOwnConnectionRemovalStarted={ownConnectionRemovalStarted} onOwnConnectionDeleted={ownConnectionDeleted} onSessionRevoked={() => {
        resetAccount(); void signOut().catch(() => {});
      }} /> : <>
    <div className="studio-intro" id="studio-top"><div><h1>새 방송 만들기</h1><p>영상 하나로, 여러 채널의 시청자를 만나세요.</p></div>{usage && <div className="studio-storage"><div><span>내 저장 공간</span><strong>{size(usage.storage_bytes)} <small>/ {size(usage.storage_limit_bytes)}</small></strong></div><meter min={0} max={usage.storage_limit_bytes} value={usage.storage_bytes} aria-label="저장 공간 사용량" /><span>{usage.storage_reserved_bytes > 0 ? `처리 중인 파일 ${size(usage.storage_reserved_bytes)} 포함` : '원본 영상과 방송 결과를 보관합니다.'}</span></div>}</div>
    <ol className="studio-steps" aria-label="방송 준비 순서"><li className={sourceReady ? 'done' : 'current'}><a href="#source-title"><span>{sourceReady ? <Check size={16} /> : '1'}</span><div><strong>영상 준비</strong><small>{sourceReady ? '영상이 준비됐어요' : '링크 또는 파일을 추가하세요'}</small></div></a></li><li className={channelsReady ? 'done' : sourceReady ? 'current' : ''}><a href="#channels-title"><span>{channelsReady ? <Check size={16} /> : '2'}</span><div><strong>채널 선택</strong><small>{targets.length ? `${targets.length}개 선택${channelsReady ? ' · 입력 완료' : ' · 연결 정보 입력'}` : maxDestinations === 1 ? '방송할 채널을 선택하세요' : '여러 채널을 함께 선택하세요'}</small></div></a></li><li className={!pendingReason ? 'current' : ''}><a href="#publish-title"><span>3</span><div><strong>방송 시작</strong><small>바로 시작하거나 예약하세요</small></div></a></li></ol>
    {!connected && <output className="message error studio-message">서버에 다시 연결하고 있습니다.<Button variant="ghost" onClick={() => void action('reconnect', async () => { await refresh(); })}>다시 연결</Button><Button variant="ghost" onClick={() => void action('login', signIn)}>다시 로그인</Button></output>}
    {error && <p className="message error studio-message" role="alert">{error}</p>}{notice && <output className="message success studio-message"><Check size={17} />{notice}</output>}
    <div className="studio-workspace"><section className="studio-source-panel studio-panel" aria-labelledby="source-title"><div className="studio-panel-heading"><div><span className="studio-step-number">1</span><h2 id="source-title">방송할 영상</h2></div><span className={sourceReady ? 'studio-ready-tag' : 'studio-muted-tag'}>{sourceReady ? <><Check size={13} />준비됨</> : '영상 추가'}</span></div>
      <div className="studio-source-body"><div className="preview studio-preview">
        {/* oxlint-disable-next-line jsx-a11y/media-has-caption */}
        {preview?.id === mediaId ? <video src={preview.url} controls preload="metadata" aria-label="선택 영상 미리보기" onError={() => { if (mediaId) void api<Signed>(`/media/${mediaId}/preview`).then(value => setPreview({ id: mediaId, url: value.url })).catch(() => setError('미리보기 연결을 확인하세요.')); }} /> : <div className="preview-empty"><span><Video size={28} /></span><strong>{importPending ? '영상을 준비하고 있어요' : '어떤 영상으로 방송할까요?'}</strong><p>{importPending ? '가져오기와 검사가 끝나면 표시됩니다.' : '아래에서 링크나 MP4 파일을 추가하세요.'}</p></div>}
      </div><div className="studio-selected-media"><div><strong>{selected?.name || '선택된 영상이 없습니다'}</strong><span>{selected ? <><Clock3 size={13} />{clock(selected.duration)}<span className="studio-dot" />{size(selected.bytes)}</> : '검사가 완료된 영상으로 방송할 수 있어요.'}</span></div>{selected && <span className="studio-format-tag">MP4</span>}</div>
      <div className="source-entry">
        <fieldset className="source-mode studio-source-tabs"><legend className="sr-only">원본 영상 추가 방식</legend>
          <button type="button" aria-pressed={sourceMode === 'library'} onClick={() => setSourceMode('library')}><Library size={16} />보관함</button>
          <button type="button" aria-pressed={sourceMode === 'link'} onClick={() => setSourceMode('link')}><LinkIcon size={16} />영상 링크</button>
          <button type="button" aria-pressed={sourceMode === 'file'} onClick={() => setSourceMode('file')}><Upload size={16} />파일 업로드</button>
        </fieldset>
        {sourceMode === 'library' ? <div className="studio-library"><div className="studio-library-heading"><strong>내 영상</strong><span>{media.length}개</span></div>
          <ul className="studio-media-list" aria-label="송출할 영상 선택">{media.map(item => <li key={item.id} className={mediaId === item.id ? 'is-selected' : ''}>
            <button type="button" className="studio-media-choice" aria-pressed={mediaId === item.id} aria-label={`${item.name} 선택`} disabled={!!busy || importPending || item.status !== 'ready'} onClick={() => { mediaSelection.select(); setMediaId(item.id); setConfirmed(false); }}><span className="studio-file-icon"><Video size={18} /></span><span><strong>{item.name}</strong><small>{item.status === 'ready' ? `${clock(item.duration)} · ${size(item.bytes)}` : states[item.status] || item.status}</small>{item.error_code && <small className="failure">{failures[item.error_code] || '영상을 준비하지 못했습니다. 다시 추가해 주세요.'}</small>}</span>{mediaId === item.id && <Check size={17} className="studio-media-check" aria-hidden="true" />}</button>
            {canOperate && <button type="button" className="studio-delete" aria-label={`${item.name} 삭제`} disabled={!!busy || ['importing', 'validating', 'pending'].includes(item.status)} onClick={() => void action('delete', async () => { await api(`/media/${item.id}`, { method: 'DELETE' }); setMedia(current => current.filter(value => value.id !== item.id)); if (item.id === importId) setImportId(''); setNotice('보관함에서 영상을 삭제했습니다.'); })}><Trash2 size={16} /></button>}
          </li>)}</ul>{!media.length && <div className="studio-library-empty"><Library size={24} /><p>아직 보관한 영상이 없어요.</p><button type="button" onClick={() => setSourceMode('link')}>영상 링크로 추가하기 <ArrowRight size={14} /></button></div>}
          <p className="hint studio-retention">영상과 결과 파일은 {health?.retention_days ?? '—'}일 동안 보관됩니다.</p></div> : sourceMode === 'link' ? <form className="source-import-form" onSubmit={importSource}>
          <LocalImportConnection key={sessionIdentity()} onLoadCode={localImporter.readPairingCode} connected={localConnected} busy={busy === 'local-connect'} disabled={!canOperate || (!!busy && busy !== 'local-connect')}
            onConnect={connectLocal} onDisconnect={() => { void localImporter.disconnect(); setLocalConnected(false); }} />
          <label htmlFor="source-provider">원본 영상 플랫폼</label>
          <select id="source-provider" value={sourceProvider} disabled={!sourceCatalog || !!busy || importPending}
            onChange={event => setSourceProvider(event.target.value)}>
            {sourceCatalog?.sources.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
          <p className="hint">원본 영상이 올라가 있는 플랫폼을 선택하세요.</p>
          <label htmlFor="source-url">녹화 영상 링크</label>
          <Input id="source-url" type="url" autoComplete="off" autoCapitalize="none" spellCheck={false} required maxLength={4096}
            aria-describedby="source-link-help" placeholder={sourceProvider === 'direct' ? 'https://…/recording.mp4' : 'https://…'}
            value={sourceUrl} disabled={!!busy || importPending} onChange={event => setSourceUrl(event.target.value)} />
          <div id="source-link-help" className="source-link-help"><p className="hint">{sourceProvider === 'youtube' ? '이 컴퓨터에서 공개 영상을 가져옵니다. 영상에 따라 로그인·봇 확인으로 제한될 수 있습니다.' : sourcePlatform?.note || '공개된 녹화 영상 한 편의 링크 또는 HTTPS MP4 다운로드 링크를 입력하세요.'}</p>
            {health && <p className="hint">영상당 최대 {clock(Math.min(health.max_duration_seconds, 120))} · {Math.min(health.max_upload_mb, 50)} MB</p>}
            {sourceProvider === 'youtube' && <details><summary><Download size={14} />내 YouTube 영상을 가져오지 못할 때<ChevronDown size={14} /></summary><p className="hint">YouTube Studio → 콘텐츠 → 해당 영상의 메뉴(⋮) → 다운로드에서 MP4를 저장한 뒤, 파일 업로드를 선택하세요. 현재 길이·용량 제한을 넘는 영상은 편집 후 업로드해 주세요.</p><p className="hint">Replay Live의 Google 로그인으로 YouTube의 서버 접근 제한이 해제되지는 않습니다.</p><a className="source-retry-link" href="https://support.google.com/youtube/answer/56100?hl=ko" target="_blank" rel="noreferrer">YouTube 공식 다운로드 안내 <ArrowUpRight size={13} aria-hidden="true" /></a></details>}
            <details><summary><CircleHelp size={14} />가져올 수 있는 영상 안내<ChevronDown size={14} /></summary><p className="hint">공개 녹화 영상 한 편을 가져올 수 있습니다. 생방송·재생목록이나 접근이 제한된 영상은 원본 MP4 파일을 업로드하세요.</p><p className="hint">Google 앱 로그인은 영상 플랫폼의 접근 권한과 별개입니다. 비공개 원본은 접근 가능한 MP4 다운로드 링크(만료 전)를 입력하거나 파일로 업로드하세요.</p></details>
          </div>
          <details className="studio-optional-field"><summary>보관함 이름 지정 <span>선택</span><ChevronDown size={14} /></summary><label htmlFor="source-name">보관함 이름 <span className="optional">선택</span></label>
          <Input id="source-name" maxLength={180} placeholder="예: 신제품 소개 녹화본" value={sourceName} disabled={!!busy || importPending}
            onChange={event => setSourceName(event.target.value)} /></details>
          <Button type="submit" className="source-import-button" disabled={!localConnected || !canOperate || !!busy || !connected || !health || !sourcePlatform || !sourceUrl.trim() || importPending}>
            {busy === 'import' ? <LoaderCircle size={16} className="source-spinner" aria-hidden="true" /> : <Download size={16} aria-hidden="true" />}
            {busy === 'import' ? localPhase === 'requesting' ? '가져오기 요청 중…' : localPhase === 'uploading' ? '내 컴퓨터에서 보관함에 업로드 중…' : localPhase === 'validating' ? '업로드 완료 확인 중…' : '내 컴퓨터에서 가져오는 중…' : importPending ? '영상 검사 중…' : '영상 가져오기'}
          </Button>
          {busy === 'import' && <output className="local-import-progress"><span>이 창과 도우미를 켜두세요.</span><button type="button" onClick={() => localImportAbort.current?.abort(new DOMException('가져오기 취소', 'AbortError'))}>가져오기 취소</button></output>}
          {importFailure && <LocalImportFailure failure={importFailure} disabled={!!busy || !canOperate || !connected || importPending}
            onRetry={() => void importSource()} onUpload={() => { setSourceMode('file'); setImportFailure(null); }} />}
          {!sourceCatalog && <p className="hint failure">원본 플랫폼 목록을 불러오지 못했습니다.<button type="button" className="source-retry-link" disabled={!!busy} onClick={() => void action('reconnect', refresh)}>다시 불러오기</button></p>}
        </form> : <div className="source-file-upload"><span className="studio-upload-symbol"><Upload size={27} /></span><h3>기기에 저장된 영상으로 시작</h3><p>원본 MP4 파일을 선택해 주세요.</p>
          <p className="hint">MP4 · 최대 {health?.max_upload_mb ?? '—'} MB{health ? ` · ${clock(health.max_duration_seconds)} 이내` : ''}</p>
          <Button variant="outline" disabled={!canOperate || !!busy || !connected || !health || importPending} onClick={() => picker.current?.click()}><Upload size={16} />{busy === 'upload' ? `${Math.round(uploadProgress)}% 업로드 중` : 'MP4 파일 선택'}</Button>
        </div>}
        <input type="file" ref={picker} accept=".mp4,video/mp4" hidden onChange={event => void upload(event.target.files?.[0])} />
        {busy === 'upload' && <Progress value={uploadProgress} aria-label="영상 업로드 진행률" />}
        {importPending && <output className="source-import-status" aria-live="polite"><LoaderCircle size={18} className="source-spinner" aria-hidden="true" /><span><strong>{states[importedMedia?.status || (preparationKind === 'upload' ? 'validating' : 'importing')] || '영상 준비 중'}</strong><small>준비와 검사가 끝나면 이 영상이 자동으로 선택됩니다. 송출 대상은 미리 설정할 수 있습니다.</small></span></output>}
        {importedMedia && ['failed', 'stopped'].includes(importedMedia.status) && <div className="source-import-status failed" role="alert"><div><strong>영상을 준비하지 못했습니다.</strong><p>{failures[importedMedia.error_code || ''] || (preparationKind === 'upload' ? '업로드한 영상이 재생 가능한 MP4인지 확인하고 다시 선택하세요.' : '링크의 공개 여부와 유효 기간을 확인하세요. 플랫폼에서 접근을 제한할 수도 있습니다.')}</p><div className="source-import-actions">{preparationKind === 'import' && <Button size="sm" variant="outline" disabled={!canOperate || !!busy || !connected} onClick={() => { setSourceMode('link'); void importSource(); }}>현재 링크로 다시 가져오기</Button>}<Button size="sm" variant="ghost" onClick={() => setSourceMode('file')}>파일로 업로드</Button></div></div></div>}
      </div>
      </div>
    </section><div className="studio-broadcast-column"><form id="broadcast-form" onSubmit={create} noValidate>
      <section className="studio-panel studio-channels-panel" aria-labelledby="channels-title"><div className="studio-panel-heading"><div><span className="studio-step-number">2</span><h2 id="channels-title">방송할 채널</h2></div><span className="studio-muted-tag">{maxDestinations === 1 ? '플랫폼별 송출 테스트' : '여러 채널 동시 방송'}</span></div><div className="studio-panel-body">
        <PlatformPicker catalog={catalog} targets={targets} destinations={destinations} onToggle={toggleTarget} onDestinationChange={(id, value) => { connectionAutofill.edit(id); setDestinations(current => ({ ...current, [id]: value })); setConfirmed(false); }} disabled={!!busy || !canOperate} maxDestinations={maxDestinations}
          savedConnections={{ location: connectionStore?.location ?? streamConnectionLocation(), ready: !!connectionStore,
            saved: savedConnections, loads: connectionLoads, error: connectionStorageError, onSave: id => void saveConnection(id), onUse: id => connectionAutofill.retry(id), onRemove: id => void removeConnection(id) }}
          localDetails={outputEstimate?.media_id === mediaId ? <div className="studio-output-estimate"><div><span>예상 결과 파일</span><strong>{size(outputEstimate.estimated_output_bytes)}</strong></div><p>사용 가능한 공간 {size(outputEstimate.storage_available_bytes)}</p>{!outputEstimate.can_create && <p className="failure">{outputEstimate.reason === 'OUTPUT_LIMIT_EXCEEDED' ? '파일당 용량을 초과합니다. 더 짧은 영상을 선택하세요.' : '저장 공간이 부족합니다. 보관함을 정리해 주세요.'}</p>}</div> : undefined} />
      </div></section>
      <section className="studio-panel studio-publish-panel" aria-labelledby="publish-title"><div className="studio-panel-heading"><div><span className="studio-step-number">3</span><h2 id="publish-title">방송 시작 설정</h2></div></div><div className="studio-panel-body">
        <label className="studio-label" htmlFor="commercial-title">방송 이름</label><Input id="commercial-title" value={title} onChange={event => setTitle(event.target.value)} placeholder="예: 주말 다시보기 라이브" maxLength={120} required disabled={!!busy || !canOperate} aria-describedby="broadcast-title-help" /><p className="hint" id="broadcast-title-help">내 방송 이력에서 구분할 이름이에요.</p>
        <fieldset className="studio-start-mode"><legend>언제 시작할까요?</legend><div><button type="button" aria-pressed={startMode === 'now'} disabled={!!busy || !canOperate} onClick={() => { setStartMode('now'); setSchedule(''); }}><Play size={17} /><span><strong>지금 시작</strong><small>준비되면 바로 방송</small></span>{startMode === 'now' && <Check size={16} />}</button><button type="button" aria-pressed={startMode === 'scheduled'} disabled={!!busy || !canOperate} onClick={() => setStartMode('scheduled')}><CalendarClock size={18} /><span><strong>예약하기</strong><small>원하는 날짜와 시간에</small></span>{startMode === 'scheduled' && <Check size={16} />}</button></div></fieldset>
        {startMode === 'scheduled' && <div className="studio-schedule-field"><label className="studio-label" htmlFor="commercial-schedule">방송 시작 시간</label><Input id="commercial-schedule" type="datetime-local" value={schedule} onChange={event => setSchedule(event.target.value)} required disabled={!!busy || !canOperate} /><p className="hint">현재 기기의 시간대를 기준으로 예약합니다. 화면을 닫아도 예약은 유지돼요.</p></div>}
        {hasLive && <label className="studio-live-confirm" htmlFor="live-readiness" aria-label="선택한 채널의 라이브 준비를 마쳤어요."><input id="live-readiness" type="checkbox" checked={confirmed} disabled={!!busy || !canOperate} onChange={event => setConfirmed(event.target.checked)} /><span><strong>선택한 채널의 라이브 준비를 마쳤어요.</strong><small>시작하면 이 영상이 선택한 채널에 라이브로 방송됩니다.</small></span></label>}
      </div></section>
    </form></div></div>
    <section className="studio-history" id="broadcast-history" aria-labelledby="history-title"><div className="studio-history-heading"><div><h2 id="history-title">방송 이력 <span>{jobs.length}</span></h2><p>전송 완료는 플랫폼의 공개·다시보기 저장 확인을 뜻하지 않습니다.</p></div><fieldset className="studio-history-filters"><legend className="sr-only">방송 이력 필터</legend>{([{ id: 'all', label: '전체' }, { id: 'active', label: '진행 중' }, { id: 'finished', label: '종료' }] as const).map(filter => <button type="button" key={filter.id} aria-pressed={historyFilter === filter.id} onClick={() => setHistoryFilter(filter.id)}>{filter.label}</button>)}</fieldset></div>
      {visibleJobs.length ? <div className="studio-job-list">{visibleJobs.map(job => <article key={job.id} className="studio-job">
        <span className="studio-job-icon"><Radio size={19} /></span>
        <div className="studio-job-main"><button type="button" className="job-title" aria-label={`${job.title} 실행 기록`} onClick={() => void action('events', async () => setEvents(await api(`/broadcasts/${job.id}/events`)))}>{job.title}<ArrowUpRight size={13} /></button>
          <p>{catalog?.targets.find(target => target.id === job.target)?.label || job.target}<span className="studio-dot" />{new Date(job.scheduled * 1000).toLocaleString('ko-KR', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</p>
          <p className="studio-job-source"><Video size={12} aria-hidden="true" /><span>원본: {job.media_name || '영상 정보 없음'}</span></p>
        </div>
        <span className={`status ${job.state}`}>{job.state === 'completed' && job.target !== 'local' ? '전송 완료' : states[job.state] || job.state}</span>
        <div className="studio-job-progress"><span>{clock(job.progress)} <small>/ {clock(job.duration)}</small></span><progress value={Math.min(job.progress, job.duration)} max={job.duration || 1} aria-label={`${job.title} 송출 진행률`} />{job.error_code && <small className="failure">{failures[job.error_code] || '송출을 마치지 못했습니다. 실행 기록을 확인해 주세요.'}</small>}</div>
        <div className="studio-job-action">{!terminal.has(job.state) && canOperate ? <Button size="sm" variant="outline" disabled={!!busy} onClick={() => void action('stop', async () => { const stopped = await api<Job>(`/broadcasts/${job.id}/stop`, { method: 'POST' }); setJobs(current => current.map(item => item.id === stopped.id ? stopped : item)); })}><Square size={12} />{job.state === 'scheduled' ? '예약 취소' : '중지'}</Button> : job.state === 'completed' && job.target === 'local' ? <Button size="sm" variant="ghost" onClick={() => void action('download', async () => { const result = await api<Signed>(`/broadcasts/${job.id}/output`); const link = document.createElement('a'); link.href = result.url; link.rel = 'noreferrer'; link.download = `replay-${job.id}.flv`; link.click(); })}><Download size={15} />결과 받기</Button> : null}</div>
        <BroadcastWatchLinks job={job} canEdit={canOperate} disabled={!!busy} onSave={links => saveWatchLinks(job, links)} />
      </article>)}</div> : <div className="studio-history-empty"><span><History size={25} /></span><div><strong>{historyFilter === 'all' ? '첫 방송을 준비해 보세요' : historyFilter === 'active' ? '진행 중인 방송이 없습니다' : '아직 종료된 방송이 없습니다'}</strong><p>{historyFilter === 'all' ? '위에서 영상과 채널을 선택하면 여기에 방송 이력이 표시됩니다.' : '다른 목록에서 방송 상태를 확인할 수 있어요.'}</p></div></div>}
    </section>
    {!!events.length && <section className="event-log studio-event-log"><div><h2>실행 기록</h2><button type="button" onClick={() => setEvents([])} aria-label="실행 기록 닫기"><X size={16} /></button></div><ol>{events.map((event, index) => <li key={index}><time>{new Date(event.at * 1000).toLocaleTimeString('ko-KR')}</time><span>{event.message || event.code}</span></li>)}</ol></section>}
    <footer className="studio-footer">Replay Live<span>예약한 방송은 화면을 닫아도 실행됩니다.</span></footer>
    <div className="studio-launch-dock" aria-label="방송 준비 요약"><div className="studio-launch-inner"><div className="studio-launch-summary"><span className="studio-launch-icon"><Radio size={21} /></span><div><strong>{targets.length ? `${targets.length}개 ${hasLive ? '채널에 방송' : '파일 테스트'} ${startMode === 'scheduled' ? '예약' : '준비'}` : '방송할 채널을 선택하세요'}</strong><p>{sourceReady ? selected?.name : '영상 선택 필요'}<span className="studio-dot" />{startDescription}</p></div></div><div className="studio-launch-action"><p id="launch-hint" className={pendingReason ? '' : 'ready'} aria-live="polite">{pendingReason || '모든 준비가 끝났어요.'}</p><Button className="studio-launch-button" type="submit" form="broadcast-form" disabled={!!busy || !!pendingReason} aria-describedby="launch-hint">{busy === 'create' ? <LoaderCircle size={17} className="source-spinner" /> : startMode === 'scheduled' ? <CalendarClock size={17} /> : <Play size={17} fill="currentColor" />}{busy === 'create' ? '방송 등록 중…' : startMode === 'scheduled' ? '방송 예약하기' : '송출 시작'}{busy !== 'create' && <ArrowRight size={16} />}</Button></div></div></div>
    </>}
  </main>;
}
