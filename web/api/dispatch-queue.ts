import { handleCallback } from '@vercel/queue';
import dispatcher from './dispatch.js';
import { consumeDispatchWakeup } from '../lib/dispatch-wakeup.js';

// This function must have a private queue/v2beta experimentalTrigger. The SDK
// parses CloudEvents; the Vercel queue trigger supplies the access boundary.
const callback = handleCallback<unknown>(
  message => consumeDispatchWakeup(message, request => dispatcher.fetch(request)),
  { visibilityTimeoutSeconds: 300 },
);

const queueHandler = { fetch: callback };
export default queueHandler;
