'use client';

import { useState } from 'react';
import { ArrowUpRight, CircleHelp, X } from 'lucide-react';
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from '@/components/ui/popover';

export default function SourceLinkHelp({ provider, note }: { provider: string; note?: string }) {
  const [open, setOpen] = useState(false);
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger type="button" className="source-help-trigger" aria-label="영상 링크 도움말">
      <CircleHelp size={17} aria-hidden="true" />
    </PopoverTrigger>
    <PopoverContent className="source-help-popover" align="start" sideOffset={8}>
      <div className="source-help-heading"><PopoverTitle>영상 링크 안내</PopoverTitle>
        <button type="button" className="source-help-close" aria-label="영상 링크 도움말 닫기" onClick={() => setOpen(false)}><X size={16} aria-hidden="true" /></button>
      </div>
      <p>공개 녹화 영상 한 편이나 HTTPS MP4 링크를 사용할 수 있어요. 생방송·재생목록은 지원하지 않아요.</p>
      {provider !== 'youtube' && note && <p>{note}</p>}
      <div className="source-help-section"><h3>도우미는 언제 필요한가요?</h3>
        <p>‘영상 가져오기’를 누르면 이 PC의 도우미에 연결해요. 연결할 수 없으면 설치·실행 안내가 열려요.</p>
      </div>
      <div className="source-help-section"><h3>가져오지 못한다면</h3>
        <p>{provider === 'youtube'
          ? 'YouTube의 로그인·봇 확인으로 제한될 수 있어요. Replay Live 로그인과는 별개예요.'
          : '접근이 제한된 영상은 원본 MP4를 업로드하거나, 접근 가능한 MP4 링크를 사용해 주세요.'}</p>
        {provider === 'youtube' && <><p>본인 영상은 YouTube Studio에서 MP4로 저장한 뒤 ‘파일 업로드’를 이용하세요.</p>
          <a href="https://support.google.com/youtube/answer/56100?hl=ko" target="_blank" rel="noreferrer">YouTube 다운로드 방법<ArrowUpRight size={13} aria-hidden="true" /></a></>}
      </div>
    </PopoverContent>
  </Popover>;
}
