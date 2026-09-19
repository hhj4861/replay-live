import { createBlobControl } from '../lib/blob-control.js';

// Store credentials remain in this control function, never in a worker grant.
const blobControlHandler = { fetch: createBlobControl() };
export default blobControlHandler;
