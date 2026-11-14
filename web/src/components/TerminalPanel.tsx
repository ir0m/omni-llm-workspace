import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { api } from '../api'

export default function TerminalPanel({ project }: { project: string }) {
  const host = useRef<HTMLDivElement>(null)
  const socket = useRef<WebSocket | null>(null)
  const terminal = useRef<Terminal | null>(null)
  const [enabled, setEnabled] = useState(false)
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState('確認中…')
  useEffect(() => {
    let live = true
    api.terminal(project).then((data) => { if (live) { setEnabled(data.enabled); setStatus(data.enabled ? '未接続' : '端末は無効です。OMNI_ALLOW_TERMINAL=true の設定が必要です。') } })
      .catch((e) => { if (live) setStatus(e.message) })
    return () => { live = false }
  }, [project])
  useEffect(() => {
    if (!open || !host.current) return
    const term = new Terminal({ fontSize: 13, cursorBlink: true, scrollback: 2000, disableStdin: true, theme: { background: '#101014' } })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host.current)
    terminal.current = term
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/projects/${encodeURIComponent(project)}/terminal/ws`)
    ws.binaryType = 'arraybuffer'
    socket.current = ws
    setStatus('接続中…')
    const resize = () => {
      if (!host.current?.clientWidth || !host.current?.clientHeight) return
      fit.fit()
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }
    ws.onopen = () => { term.options.disableStdin = false; setStatus('接続中'); resize(); term.focus() }
    ws.onmessage = (event) => term.write(new Uint8Array(event.data))
    ws.onclose = (event) => { term.options.disableStdin = true; setStatus(event.code === 1000 ? '終了しました。再接続するには一度「終了」を押してください' : '切断されました。ログイン状態と端末の許可を確認して再接続してください'); socket.current = null }
    ws.onerror = () => setStatus('接続できません。ログイン状態と端末の許可を確認してください')
    const input = term.onData((data) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data })) })
    const observer = new ResizeObserver(resize)
    observer.observe(host.current)
    return () => {
      observer.disconnect(); input.dispose(); ws.onclose = null; ws.onerror = null; ws.onmessage = null; ws.onopen = null
      ws.close(); socket.current = null; terminal.current = null; term.dispose()
    }
  }, [open, project])
  function key(data: string) {
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify({ type: 'input', data }))
    terminal.current?.focus()
  }
  return <div className="flex min-h-0 flex-1 flex-col" onKeyDown={(e) => e.stopPropagation()}>
    <div className="flex items-center gap-2 p-2 text-xs">
      <span role="status" className="flex-1">{status}</span>
      <button disabled={!enabled && !open} className="rounded border border-border px-3 py-2 disabled:opacity-50" onClick={() => setOpen(!open)}>{open ? '終了' : '接続'}</button>
    </div>
    <p className="px-2 pb-2 text-xs text-muted">サーバーユーザーとして直接操作します。タブの切り替え・画面を閉じる操作で端末内の実行も終了します。</p>
    <div ref={host} className="min-h-0 flex-1 overflow-hidden bg-[#101014] p-1" />
    <div className="grid grid-cols-8 gap-1 p-2">
      {([['Ctrl+C', '\x03'], ['Ctrl+D', '\x04'], ['Esc', '\x1b'], ['Tab', '\t'], ['↑', '\x1b[A'], ['↓', '\x1b[B'], ['←', '\x1b[D'], ['→', '\x1b[C']] as const).map(([label, data]) =>
        <button key={label} onClick={() => key(data)} className="min-h-10 rounded border border-border px-1 py-2 text-xs">{label}</button>)}
    </div>
  </div>
}
