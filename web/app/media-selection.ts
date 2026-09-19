type Item = { id: string; status: string };
type Ticket = { identity: string; generation: number; id?: string };

// Only the most recent upload/import intent may choose a video when polling
// observes validation completion. Failed preparation never chooses an older file.
export function createMediaSelection() {
  let generation = 0;
  let explicit = false;
  let pending: Ticket | undefined;
  return {
    capture: () => generation,
    begin(identity: string) { explicit = true; pending = { identity, generation: ++generation }; return pending; },
    register(ticket: Ticket, id: string) {
      if (pending !== ticket || ticket.generation !== generation) return false;
      ticket.id = id; return true;
    },
    select() { generation += 1; explicit = true; pending = undefined; },
    reset() { generation += 1; explicit = false; pending = undefined; },
    reconcile(items: Item[], identity: string, requestGeneration: number) {
      if (requestGeneration !== generation || (pending && pending.identity !== identity)) return { kind: 'stale' } as const;
      if (pending) {
        const item = items.find(value => value.id === pending?.id);
        if (item?.status === 'ready') { pending = undefined; return { kind: 'ready', id: item.id } as const; }
        if (item && ['failed', 'stopped'].includes(item.status)) { pending = undefined; return { kind: 'failed' } as const; }
        return { kind: 'pending' } as const;
      }
      return { kind: 'default', allowFirst: !explicit } as const;
    },
  };
}
