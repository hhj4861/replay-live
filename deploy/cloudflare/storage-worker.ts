import { storageControl, storageObject, type StorageEnv } from './storage';

// Storage can recover independently of the API/FFmpeg Containers rollout.
export default {
  fetch(request: Request, env: StorageEnv) {
    const path = new URL(request.url).pathname;
    if (path === '/api/blob-control') return storageControl(request, env);
    if (path.startsWith('/objects/')) return storageObject(request, env);
    return Response.json({ code: 'NOT_FOUND' }, { status: 404 });
  },
};
