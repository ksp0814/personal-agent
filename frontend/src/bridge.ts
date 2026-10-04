import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

export type WorkspaceSnapshot = {
  files: string[]
  directories: string[]
  git: { available: boolean; branch: string; entries: Record<string, string> }
  rollbackWarning?: string | null
}

export async function getWorkspaceSnapshot(workspace: string): Promise<WorkspaceSnapshot> {
  return invoke<WorkspaceSnapshot>('bridge_request', {
    workspace,
    request: { method: 'workspace_snapshot' },
  })
}

export type UsageSnapshot = {
  rateLimits?: { primary?: { usedPercent?: number; resetsAt?: number } }
  primary?: { usedPercent?: number; resetsAt?: number }
  rateLimitResetCredits?: { availableCount?: number; credits?: Array<{ id?: string }> }
}

export async function getUsageSnapshot(workspace: string): Promise<UsageSnapshot> {
  return invoke<UsageSnapshot>('bridge_request', {
    workspace,
    request: { method: 'usage_snapshot' },
  })
}

export function resetUsage(workspace: string, creditId = '', idempotencyKey = '') {
  return invoke<{ outcome: string }>('bridge_request', {
    workspace,
    request: { method: 'usage_reset', creditId, idempotencyKey },
  })
}

export type FileContent = { path: string; content: string }

export async function readFile(workspace: string, path: string): Promise<FileContent> {
  return invoke<FileContent>('bridge_request', {
    workspace,
    request: { method: 'read_file', path },
  })
}

export async function readDiff(workspace: string, path: string): Promise<FileContent> {
  return invoke<FileContent>('bridge_request', {
    workspace,
    request: { method: 'git_diff_file', path },
  })
}

export function approveFile(workspace: string, path: string) {
  return invoke<{ path: string; action: string }>('bridge_request', {
    workspace,
    request: { method: 'approve_file', path },
  })
}

export function rollbackFile(workspace: string, path: string) {
  return invoke<{ path: string; action: string; location: string; recoveryLocation?: string | null }>('bridge_request', {
    workspace,
    request: { method: 'rollback_file', path },
  })
}

export type TerminalEvent = {
  session_id: string
  event: 'ready' | 'output' | 'error' | 'exit' | 'stopped'
  data?: string
  message?: string
}

type TerminalEventListener = (event: TerminalEvent) => void
const terminalListeners = new Map<string, Set<TerminalEventListener>>()
let terminalEventRouter: Promise<void> | undefined

function startTerminalEventRouter(): Promise<void> {
  if (!terminalEventRouter) {
    terminalEventRouter = listen<TerminalEvent>('terminal-event', (event) => {
      const listeners = terminalListeners.get(event.payload.session_id)
      listeners?.forEach((listener) => listener(event.payload))
    }).then(() => undefined).catch((error: unknown) => {
      terminalEventRouter = undefined
      throw error
    })
  }
  return terminalEventRouter
}

export function subscribeTerminalEvents(sessionId: string, listener: TerminalEventListener) {
  const ready = startTerminalEventRouter()
  const listeners = terminalListeners.get(sessionId) ?? new Set<TerminalEventListener>()
  listeners.add(listener)
  terminalListeners.set(sessionId, listeners)
  return {
    ready,
    unsubscribe: () => {
      listeners.delete(listener)
      if (listeners.size === 0) terminalListeners.delete(sessionId)
    },
  }
}

export function startTerminal(sessionId: string, workspace: string) {
  return invoke<boolean>('start_terminal', { sessionId, workspace })
}

export function readTerminalBuffer(sessionId: string) {
  return invoke<string>('terminal_buffer', { sessionId })
}

export function writeTerminal(sessionId: string, text: string) {
  return invoke<void>('write_terminal', { sessionId, text })
}

export function resizeTerminal(sessionId: string, columns: number, rows: number) {
  return invoke<void>('resize_terminal', { sessionId, columns, rows })
}

export function stopTerminal(sessionId: string) {
  return invoke<void>('stop_terminal', { sessionId })
}
