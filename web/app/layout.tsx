import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Replay Live | 녹화 영상 송출',
  description: '녹화 MP4를 예약하고 YouTube Live로 송출하는 로컬 POC',
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="ko"><body>{children}</body></html>;
}
