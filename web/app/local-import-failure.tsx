import { useEffect, useRef } from 'react';

export type ImportFailure = { title: string; message: string; connect: boolean };

export default function LocalImportFailure({ failure, disabled, onRetry, onUpload }: {
  failure: ImportFailure; disabled: boolean; onRetry: () => void; onUpload: () => void;
}) {
  const alert = useRef<HTMLDivElement>(null);
  useEffect(() => {
    alert.current?.focus({ preventScroll: true });
    alert.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [failure]);
  return <div ref={alert} className="source-import-status failed local-import-failure" role="alert" tabIndex={-1}>
    <div><strong>{failure.title}</strong><p>{failure.message}</p>
      <div className="source-import-actions">
        {!failure.connect && <button type="button" disabled={disabled} onClick={onRetry}>현재 링크로 다시 가져오기</button>}
        <button type="button" disabled={disabled} onClick={onUpload}>MP4 파일로 업로드</button>
      </div>
    </div>
  </div>;
}
