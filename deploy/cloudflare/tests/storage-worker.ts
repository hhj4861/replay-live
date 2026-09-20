import { storageControl, storageObject, type StorageEnv } from '../storage';
export default {
  fetch(request: Request, env: StorageEnv) {
    return new URL(request.url).pathname === '/api/blob-control'
      ? storageControl(request, env) : storageObject(request, env);
  },
};
