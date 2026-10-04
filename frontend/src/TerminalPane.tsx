import { memo, useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { readTerminalBuffer, resizeTerminal, startTerminal, subscribeTerminalEvents, writeTerminal, type TerminalEvent } from './bridge'
import '@xterm/xterm/css/xterm.css'

type Props = {
  sessionId: string
  workspace: string
}

export const TerminalPane = memo(function TerminalPane({ sessionId, workspace }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const terminalId = `${workspace}::${sessionId}`

  useEffect(() => {
    if (!containerRef.current) return
    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: '"Cascadia Code", "SFMono-Regular", Consolas, monospace',
      fontSize: 13,
      theme: {
        background: '#0d0f10',
        foreground: '#d9e4dd',
        cursor: '#8be0b7',
        selectionBackground: '#315b47',
        black: '#1b211e', red: '#f07178', green: '#7ee2a8', yellow: '#e6c17a',
        blue: '#82aaff', magenta: '#c792ea', cyan: '#89ddff', white: '#d9e4dd',
        brightBlack: '#526158', brightRed: '#ff8b94', brightGreen: '#a6f3c2', brightYellow: '#f4d99b',
        brightBlue: '#a8c7ff', brightMagenta: '#e2b8ff', brightCyan: '#b8efff', brightWhite: '#f5faf7',
      },
    })
    const fit = new FitAddon()
    terminal.loadAddon(fit)
    terminal.open(containerRef.current)
    fit.fit()
    if (terminal.textarea) {
      terminal.textarea.setAttribute('autocomplete', 'off')
      terminal.textarea.setAttribute('autocorrect', 'off')
      terminal.textarea.setAttribute('autocapitalize', 'off')
      terminal.textarea.setAttribute('spellcheck', 'false')
    }

    let stopped = false
    let terminalReady = false
    let resizeTimer: number | undefined
    let outputTimer: number | undefined
    let pendingOutput = ''
    let lastSize = ''
    const flushOutput = () => {
      outputTimer = undefined
      if (!pendingOutput || stopped) return
      const output = pendingOutput
      pendingOutput = ''
      terminal.write(output)
    }
    const queueOutput = (output: string) => {
      pendingOutput += output
      if (outputTimer === undefined) outputTimer = window.setTimeout(flushOutput, 16)
    }
    const eventSubscription = subscribeTerminalEvents(terminalId, (payload: TerminalEvent) => {
      if (payload.event === 'output' && payload.data) queueOutput(payload.data)
      if (payload.event === 'error' && payload.message) queueOutput(`\r\n\x1b[31m${payload.message}\x1b[0m\r\n`)
    })

    const start = async () => {
      try {
        await eventSubscription.ready
        if (stopped) return
        const reused = await startTerminal(terminalId, workspace)
        if (stopped) {
          return
        }
        if (reused) {
          const bufferedOutput = await readTerminalBuffer(terminalId)
          if (bufferedOutput) queueOutput(bufferedOutput)
        }
        terminalReady = true
        await resizeTerminal(terminalId, terminal.cols, terminal.rows)
        lastSize = `${terminal.cols}x${terminal.rows}`
      } catch (error) {
        if (!stopped) terminal.write(`\x1b[31m터미널을 시작하지 못했습니다: ${String(error)}\x1b[0m\r\n`)
      }
    }
    void start()

    const isTerminalResponse = (data: string) => /^(?:\x1b\[[?0-9;]*c|\x1b\][0-9]+;rgb:[0-9a-f/]+\x1b\\)+$/i.test(data)
    const input = terminal.onData((data) => {
      if (!isTerminalResponse(data)) void writeTerminal(terminalId, data)
    })
    const focusTerminal = () => terminal.focus()
    containerRef.current.addEventListener('mousedown', focusTerminal)
    const resizeObserver = new ResizeObserver(() => {
      if (!terminalReady) return
      if (resizeTimer !== undefined) window.clearTimeout(resizeTimer)
      resizeTimer = window.setTimeout(() => {
        fit.fit()
        const size = `${terminal.cols}x${terminal.rows}`
        if (size === lastSize) return
        lastSize = size
        void resizeTerminal(terminalId, terminal.cols, terminal.rows)
      }, 80)
    })
    resizeObserver.observe(containerRef.current)

    return () => {
      stopped = true
      terminalReady = false
      eventSubscription.unsubscribe()
      input.dispose()
      if (resizeTimer !== undefined) window.clearTimeout(resizeTimer)
      if (outputTimer !== undefined) window.clearTimeout(outputTimer)
      containerRef.current?.removeEventListener('mousedown', focusTerminal)
      resizeObserver.disconnect()
      terminal.dispose()
    }
  }, [sessionId, workspace])

  return <div ref={containerRef} className="terminal-xterm" aria-label="Codex 터미널" />
})
