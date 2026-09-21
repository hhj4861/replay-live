// Native-browser verification surface: real component + real loopback pairing.
// Login controls below are explicitly simulated; never uses cloud credentials.
import { mkdtemp, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const root = fileURLToPath(new URL('..', import.meta.url));
const require = createRequire(path.join(root, 'web/package.json'));
const { createServer } = await import(require.resolve('vite'));
const { default: react } = await import(require.resolve('@vitejs/plugin-react'));
const scratch = await realpath(await mkdtemp(path.join(tmpdir(), 'replay-helper-browser-')));
await writeFile(path.join(scratch, 'index.html'), '<html lang="ko"><meta charset="utf-8"><title>Replay 도우미 연결 검증</title><div id="root"></div><script type="module" src="/main.tsx"></script></html>');
await writeFile(path.join(scratch, 'main.tsx'), `
import React, {useCallback, useState} from 'react';
import {createRoot} from 'react-dom/client';
import LocalImportConnection from '@/app/local-import-connection';
import {createLocalImporter} from '@/lib/local-import';
function App() {
 const [signedIn, setSignedIn] = useState(false);
 const [client] = useState(createLocalImporter);
 const [connected, setConnected] = useState(false);
 const [offline, setOffline] = useState(false);
 const pair = useCallback(async (code, signal) => {await client.pair(code,signal); setConnected(client.isConnected());},[client]);
 const discover = useCallback(signal => offline ? Promise.reject(new Error('검증용 도우미 미실행 상태')) : client.readPairingCode(signal),[client,offline]);
 return <main style={{maxWidth:900,margin:'60px auto',fontFamily:'sans-serif',padding:24}}>
 <h1>도우미 연결 브라우저 검증</h1><p>로그인은 검증용이며, 연결은 이 PC의 실제 도우미를 사용합니다. 영상 다운로드나 운영 계정 접근은 하지 않습니다.</p>
 {signedIn ? <><button onClick={() => {void client.disconnect();setConnected(false);setSignedIn(false);}}>검증용 로그아웃</button>
 <button onClick={() => {void client.disconnect();setConnected(false);setOffline(!offline);}}>{offline?'실제 도우미로 복귀':'미실행 상태 재현'}</button>
 <LocalImportConnection key={String(offline)} connected={connected} disabled={false} onLoadCode={discover} onConnect={pair} onDisconnect={() => {void client.disconnect();setConnected(false);}}/></>
 : <button onClick={() => setSignedIn(true)}>검증용 로그인</button>}
 </main>;
}
createRoot(document.getElementById('root')).render(<App/>);
`);
await writeFile(path.join(scratch, 'helper-release.json'), '{"version":null,"downloads":[]}');
const server = await createServer({ configFile: false, root: scratch, publicDir:path.join(root,'web/public'), plugins:[react()],
  define:{__REPLAY_COMMERCIAL__:'false',__REPLAY_CLOUD__:'false'},
  resolve:{alias:{'@':path.join(root,'web'),react:path.join(root,'web/node_modules/react'), 'react-dom':path.join(root,'web/node_modules/react-dom')}},
  server:{host:'127.0.0.1',port:3100,strictPort:true,fs:{allow:[root,scratch,await realpath(path.join(root,'web/node_modules'))]}}});
await server.listen();
console.log('Native Chrome verification: http://127.0.0.1:3100');
for (const signal of ['SIGINT','SIGTERM']) process.on(signal,async()=>{await server.close();await rm(scratch,{recursive:true,force:true});process.exit(0);});
