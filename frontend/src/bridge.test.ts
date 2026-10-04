import { beforeEach, describe, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import { getWorkspaceSnapshot, readDiff, resetUsage, startTerminal, subscribeTerminalEvents, writeTerminal } from './bridge'

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn() }))

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Tauri bridge commands', () => {
  it('requests workspace data through the Python bridge', async () => {
    vi.mocked(invoke).mockResolvedValueOnce({ files: [], directories: [], git: { available: false, branch: '', entries: {} } })

    await getWorkspaceSnapshot('C:/workspace')

    expect(invoke).toHaveBeenCalledWith('bridge_request', {
      workspace: 'C:/workspace',
      request: { method: 'workspace_snapshot' },
    })
  })

  it('requests a file diff instead of plain file content', async () => {
    vi.mocked(invoke).mockResolvedValueOnce({ path: 'README.md', content: '@@ -1 +1 @@' })

    await readDiff('C:/workspace', 'README.md')

    expect(invoke).toHaveBeenCalledWith('bridge_request', {
      workspace: 'C:/workspace',
      request: { method: 'git_diff_file', path: 'README.md' },
    })
  })

  it('passes a stable idempotency key when consuming a reset credit', async () => {
    await resetUsage('C:/workspace', 'credit-1', 'request-1')

    expect(invoke).toHaveBeenCalledWith('bridge_request', {
      workspace: 'C:/workspace',
      request: { method: 'usage_reset', creditId: 'credit-1', idempotencyKey: 'request-1' },
    })
  })

  it('exposes readiness for terminal event subscription', async () => {
    let finishRegistration: (() => void) | undefined
    vi.mocked(listen).mockImplementationOnce(() => new Promise<() => void>((resolve) => {
      finishRegistration = () => resolve(() => undefined)
    }))

    const subscription = subscribeTerminalEvents('session-1', vi.fn())
    let ready = false
    void subscription.ready.then(() => { ready = true })
    await Promise.resolve()
    expect(ready).toBe(false)

    finishRegistration?.()
    await subscription.ready
    expect(ready).toBe(true)
    subscription.unsubscribe()
  })

  it('routes terminal lifecycle and input commands to Tauri', async () => {
    await startTerminal('session-1', 'C:/workspace')
    await writeTerminal('session-1', 'echo hello\r')

    expect(invoke).toHaveBeenNthCalledWith(1, 'start_terminal', { sessionId: 'session-1', workspace: 'C:/workspace' })
    expect(invoke).toHaveBeenNthCalledWith(2, 'write_terminal', { sessionId: 'session-1', text: 'echo hello\r' })
  })
})
