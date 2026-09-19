import { validateStreamConnection, type StreamConnection, type StreamConnectionStore } from './stream-connections';

export type ConnectionLoadState = { loading: boolean; error: string; appliedVersion: number };
type Entry = ConnectionLoadState & { touched: boolean; attempted: boolean; request?: object };
type Options = {
  isCurrentStore: (store: StreamConnectionStore) => boolean;
  apply: (target: string, value: StreamConnection, accept: () => boolean) => void;
};

// Tracks intent and requests, never saved credentials. Each selection gets a
// separate entry so late responses cannot survive deselection or manual edits.
export function createConnectionAutofill(initialOptions?: Options) {
  let options = initialOptions;
  let store: StreamConnectionStore | undefined;
  let saved = new Set<string>();
  const entries = new Map<string, Entry>();
  const listeners = new Set<() => void>();
  let snapshot: Record<string, ConnectionLoadState> = {};
  let appliedVersion = 0;
  function publish() {
    const next = Object.fromEntries([...entries].map(([id, entry]) => [id,
      { loading: entry.loading, error: entry.error, appliedVersion: entry.appliedVersion }]));
    if (JSON.stringify(snapshot) === JSON.stringify(next)) return;
    snapshot = next; listeners.forEach(listener => listener());
  }
  function invalidate(entry: Entry) { entry.request = undefined; entry.loading = false; entry.error = ''; }
  function select(target: string, selected: boolean) {
    if (target === 'local') return;
    if (!selected) entries.delete(target);
    else if (!entries.has(target)) entries.set(target, { touched: false, attempted: false, loading: false, error: '', appliedVersion: 0 });
    publish();
  }
  function start(target: string, manual = false) {
    const entry = entries.get(target);
    const currentStore = store;
    if (!entry || !currentStore || !saved.has(target) || !options?.isCurrentStore(currentStore)) return;
    if (!manual && (entry.touched || entry.attempted)) return;
    const request = {};
    entry.request = request; entry.attempted = true; entry.loading = true; entry.error = '';
    const accept = () => store === currentStore && entries.get(target) === entry && entry.request === request
      && saved.has(target) && !!options?.isCurrentStore(currentStore);
    publish();
    void currentStore.use(target).then(value => {
      if (!accept()) return;
      if (value.target !== target) throw new Error('Invalid saved target');
      const connection = { target, ...validateStreamConnection(value) };
      options!.apply(target, connection, accept);
      entry.loading = false; entry.appliedVersion = ++appliedVersion; publish();
    }).catch(() => {
      if (!accept()) return;
      entry.loading = false;
      entry.error = '저장한 연결을 불러오지 못했습니다. 다시 불러오거나 연결 정보를 직접 입력하세요.';
      publish();
    });
  }
  return {
    configure(nextOptions: Options) { options = nextOptions; },
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    getSnapshot: () => snapshot,
    select,
    sync(nextStore: StreamConnectionStore | undefined, selected: string[], savedTargets: string[]) {
      if (store !== nextStore) {
        store = nextStore;
        for (const entry of entries.values()) {
          if (entry.loading) { invalidate(entry); entry.attempted = false; }
        }
      }
      saved = new Set(savedTargets);
      for (const target of entries.keys()) if (!selected.includes(target)) entries.delete(target);
      selected.forEach(target => select(target, true));
      for (const [target, entry] of entries) {
        if (!saved.has(target) && entry.loading) invalidate(entry);
        start(target);
      }
      publish();
    },
    edit(target: string) {
      const entry = entries.get(target);
      if (entry) { invalidate(entry); entry.touched = true; entry.attempted = true; publish(); }
    },
    remove(target: string) {
      const entry = entries.get(target);
      if (entry) { invalidate(entry); entry.touched = true; entry.attempted = true; publish(); }
    },
    retry(target: string) { start(target, true); },
    reset() { store = undefined; saved.clear(); entries.clear(); publish(); },
  };
}
