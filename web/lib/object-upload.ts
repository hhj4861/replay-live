export type ObjectUpload = { url: string; method: string; headers: Record<string, string>; multipart?: { part_size: number } };

/** One bounded request per part; the capability authorizes only this object. */
export async function uploadObject(file: File, signed: ObjectUpload, options: {
  check: () => void; signal?: AbortSignal; progress: (percent: number) => void;
  active: (request: XMLHttpRequest | null) => void;
}) {
  const { check, signal, progress, active } = options;
  const send = (data: Blob, headers: Record<string, string>, offset: number) => new Promise<string>((resolve, reject) => {
    check();
    const xhr = new XMLHttpRequest(); xhr.open(signed.method, signed.url); xhr.timeout = 15 * 60_000;
    active(xhr);
    const abort = () => xhr.abort();
    signal?.addEventListener('abort', abort, { once: true });
    xhr.onloadend = () => { signal?.removeEventListener('abort', abort); };
    for (const [key, value] of Object.entries(headers)) if (key.toLowerCase() !== 'content-length') xhr.setRequestHeader(key, value);
    xhr.upload.onprogress = event => { if (event.lengthComputable) { try { check(); progress((offset + event.loaded) / file.size * 100); } catch { xhr.abort(); } } };
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.responseText) : reject(new Error('업로드에 실패했습니다. 다시 시도하세요.'));
    xhr.onerror = xhr.ontimeout = () => reject(new Error('업로드 연결이 끊겼습니다. 다시 시도하세요.'));
    xhr.onabort = () => reject(new DOMException('영상 업로드가 취소되었습니다.', 'AbortError'));
    xhr.send(data);
  });
  if (!signed.multipart) { try { await send(file, signed.headers, 0); check(); return; } finally { active(null); } }
  if (signed.multipart.part_size !== 32 * 1024 ** 2) throw new Error('분할 업로드 정보를 확인할 수 없습니다.');
  try {
    const parts: { partNumber: number; etag: string }[] = [];
    for (let offset = 0; offset < file.size; offset += signed.multipart.part_size) {
      check();
      const number = parts.length + 1;
      const part = JSON.parse(await send(file.slice(offset, offset + signed.multipart.part_size),
        { ...signed.headers, 'X-Replay-Part': String(number) }, offset));
      check();
      if (part.partNumber !== number || typeof part.etag !== 'string' || !part.etag || part.etag.length > 512) throw new Error('업로드한 조각을 확인하지 못했습니다.');
      parts.push({ partNumber: number, etag: part.etag });
    }
    check();
    const response = await fetch(signed.url, { method: 'POST', credentials: 'omit', redirect: 'error',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(180000)]) : AbortSignal.timeout(180000),
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parts }) });
    check(); if (!response.ok) throw new Error('파일 무결성을 확인하지 못했습니다. 다시 업로드하세요.');
  } catch (error) {
    await fetch(signed.url, { method: 'DELETE', credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(5000) }).catch(() => {});
    throw error;
  } finally { active(null); }
}
