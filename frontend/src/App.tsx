import { lazy, memo, Suspense, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { getVersion } from '@tauri-apps/api/app'
import { open } from '@tauri-apps/plugin-dialog'
import { relaunch } from '@tauri-apps/plugin-process'
import { check, type Update } from '@tauri-apps/plugin-updater'
import {
  ArrowDown, ArrowUp, Check, ChevronDown, ChevronRight, Command, FileCode2, FileSearch, Folder,
  FolderOpen, FolderPlus, GitBranch, Keyboard, Menu, MoreHorizontal, PanelLeft, Plus,
  RefreshCw, Search, SquareTerminal, TerminalSquare, X,
} from 'lucide-react'
import { buildFileTree, filterFiles, flattenFilePaths, type FileEntry, type Workspace } from './appState'
import { approveFile, getUsageSnapshot, getWorkspaceSnapshot, readDiff, readFile, resetUsage, rollbackFile, stopTerminal, type UsageSnapshot, type WorkspaceSnapshot } from './bridge'
import { loadSessions, saveSessions, type AgentSession } from './sessionState'
import './styles.css'

const TerminalPane = lazy(() => import('./TerminalPane').then(({ TerminalPane: pane }) => ({ default: pane })))

type Tab = { id: string; label: string; kind: 'session' | 'file' }
type OpenFile = { path: string; content?: string; loading: boolean; error?: string; kind?: 'file' | 'diff' }
type PaletteItem = { id: string; label: string; detail: string; kind: 'workspace' | 'session' | 'file'; action: () => void }

const MAX_SNAPSHOT_CACHE_ENTRIES = 8
const MAX_OPEN_FILES_PER_WORKSPACE = 24
const UPDATE_CHECK_INTERVAL_MS = 10 * 60 * 1000
const MIN_SIDEBAR_WIDTH = 180
const MAX_SIDEBAR_WIDTH = 420
const MIN_FILE_PANEL_WIDTH = 220
const MAX_FILE_PANEL_WIDTH = 440

function loadWorkspaces(): Workspace[] {
  try {
    const stored = JSON.parse(window.localStorage.getItem('personal-agent:workspaces') ?? '[]')
    if (Array.isArray(stored)) {
      const valid = stored.filter((item): item is Workspace => item && typeof item.path === 'string' && typeof item.name === 'string')
      if (valid.length > 0) return valid
    }
  } catch {
    // Use the default workspace when persisted state is unavailable.
  }
  return []
}

const FileTree = memo(function FileTree({ entries, depth = 0, basePath = '', onOpen }: { entries: FileEntry[]; depth?: number; basePath?: string; onOpen: (path: string) => void }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  return <div className="file-tree" role={depth === 0 ? 'tree' : undefined}>
    {entries.map((entry) => {
      const path = basePath ? `${basePath}/${entry.name}` : entry.name
      const hasChildren = Boolean(entry.children?.length)
      const isExpanded = expanded[path] ?? depth < 2
      return <div key={path}>
        <button className="tree-row" style={{ paddingLeft: `${12 + depth * 16}px` }} onClick={() => hasChildren ? setExpanded((current) => ({ ...current, [path]: !isExpanded })) : onOpen(path)} aria-expanded={hasChildren ? isExpanded : undefined} aria-level={depth + 1} role="treeitem" title={path}>
          {hasChildren ? (isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />) : <span className="tree-spacer" />}
          {entry.kind === 'folder' ? (isExpanded ? <FolderOpen size={15} /> : <Folder size={15} />) : <FileCode2 size={15} />}
          <span>{entry.name}</span>
        </button>
        {hasChildren && isExpanded && <FileTree entries={entry.children ?? []} depth={depth + 1} basePath={path} onOpen={onOpen} />}
      </div>
    })}
  </div>
})

const FileView = memo(function FileView({ file }: { file?: OpenFile }) {
  if (!file) return <div className="file-empty"><FileCode2 size={22} /><span>파일을 선택하세요</span></div>
  if (file.loading) return <div className="file-empty"><RefreshCw className="spin" size={18} /><span>파일을 읽는 중…</span></div>
  if (file.error) return <div className="file-empty file-error"><span>{file.error}</span></div>
  if (file.kind === 'diff') {
    const lines = (file.content ?? '').split('\n')
    return <div className="file-view-container"><div className="file-view-toolbar"><FileSearch size={14} /><strong>{file.path}</strong><span>변경사항</span></div><div className="file-view-shell diff-shell" aria-label={`${file.path} 변경사항`}><div className="diff-view">{lines.map((line, index) => {
      const type = line.startsWith('@@') ? 'hunk' : line.startsWith('+++') || line.startsWith('---') ? 'header' : line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : 'context'
      return <div key={`${index}-${line}`} className={`diff-line diff-${type}`}><span className="diff-line-number" aria-hidden="true">{index + 1}</span><code>{line || ' '}</code></div>
    })}</div></div></div>
  }
  return <div className="file-view-container"><div className="file-view-toolbar"><FileCode2 size={14} /><strong>{file.path}</strong><span>파일 미리보기</span></div><div className="file-view-shell"><pre className="file-content" aria-label={`${file.path} 파일 내용`}>{file.content}</pre></div></div>
})

function App() {
  const [query, setQuery] = useState('')
  const [workspaces, setWorkspaces] = useState<Workspace[]>(loadWorkspaces)
  const [workspace, setWorkspace] = useState<Workspace | null>(() => workspaces[0] ?? null)
  const [sessions, setSessions] = useState<AgentSession[]>(() => workspaces[0] ? loadSessions(workspaces[0].path, window.localStorage) : [])
  const [sessionsByWorkspace, setSessionsByWorkspace] = useState<Record<string, AgentSession[]>>(() => Object.fromEntries(workspaces.map((item) => [item.path, loadSessions(item.path, window.localStorage)])))
  const [activeSessionId, setActiveSessionId] = useState(() => workspaces[0] ? loadSessions(workspaces[0].path, window.localStorage)[0].id : '')
  const [activeTabs, setActiveTabs] = useState<Record<string, string>>({})
  const [openFilesByWorkspace, setOpenFilesByWorkspace] = useState<Record<string, Record<string, OpenFile>>>({})
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [filePanelOpen, setFilePanelOpen] = useState(true)
  const [sidebarWidth, setSidebarWidth] = useState(248)
  const [filePanelWidth, setFilePanelWidth] = useState(292)
  const [workspaceFiles, setWorkspaceFiles] = useState<FileEntry[]>([])
  const [git, setGit] = useState<WorkspaceSnapshot['git']>({ available: false, branch: 'dev', entries: {} })
  const [usage, setUsage] = useState<{ percent: number | null; resetsAt?: number; credits: number; creditId: string }>({ percent: null, credits: 0, creditId: '' })
  const [usageOpen, setUsageOpen] = useState(false)
  const [usageResetting, setUsageResetting] = useState(false)
  const [availableUpdate, setAvailableUpdate] = useState<Update | null>(null)
  const [updateInstalling, setUpdateInstalling] = useState(false)
  const [updateProgress, setUpdateProgress] = useState<number | null>(null)
  const [updateError, setUpdateError] = useState('')
  const [currentVersion, setCurrentVersion] = useState('')
  const [updateChecking, setUpdateChecking] = useState(false)
  const [workspaceMenu, setWorkspaceMenu] = useState<{ path: string; x: number; y: number } | null>(null)
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState('')
  const [paletteIndex, setPaletteIndex] = useState(0)
  const [workspaceRefreshing, setWorkspaceRefreshing] = useState(false)
  const [notice, setNotice] = useState('')
  const currentWorkspacePath = useRef<string | undefined>(workspace?.path)
  const workspaceSnapshotCache = useRef<Record<string, { snapshot: WorkspaceSnapshot; refreshedAt: number }>>({})
  const usageCache = useRef<Record<string, { snapshot: UsageSnapshot; refreshedAt: number }>>({})
  const paletteInputRef = useRef<HTMLInputElement>(null)
  const fileSearchRef = useRef<HTMLInputElement>(null)
  const noticeTimer = useRef<number | undefined>(undefined)
  const usageResetKeys = useRef<Record<string, string>>({})
  const showNotice = useCallback((message: string) => {
    setNotice(message)
    if (noticeTimer.current !== undefined) window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setNotice(''), 3200)
  }, [])

  useEffect(() => () => {
    if (noticeTimer.current !== undefined) window.clearTimeout(noticeTimer.current)
  }, [])
  const startResize = (side: 'left' | 'right', event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    const startX = event.clientX
    const initialWidth = side === 'left' ? sidebarWidth : filePanelWidth
    const minWidth = side === 'left' ? MIN_SIDEBAR_WIDTH : MIN_FILE_PANEL_WIDTH
    const maxWidth = side === 'left' ? MAX_SIDEBAR_WIDTH : MAX_FILE_PANEL_WIDTH
    const handleMove = (moveEvent: globalThis.PointerEvent) => {
      const delta = moveEvent.clientX - startX
      const nextWidth = Math.max(minWidth, Math.min(maxWidth, initialWidth + (side === 'left' ? delta : -delta)))
      if (side === 'left') setSidebarWidth(nextWidth)
      else setFilePanelWidth(nextWidth)
    }
    const stopResize = () => {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('pointermove', handleMove)
      window.removeEventListener('pointerup', stopResize)
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', handleMove)
    window.addEventListener('pointerup', stopResize, { once: true })
  }
  currentWorkspacePath.current = workspace?.path
  const openFiles = workspace ? openFilesByWorkspace[workspace.path] ?? {} : {}
  const activeTab = workspace ? activeTabs[workspace.path] ?? sessions[0]?.id ?? '' : ''
  const updateActiveTab = useCallback((tab: string) => { if (workspace) setActiveTabs((current) => ({ ...current, [workspace.path]: tab })) }, [workspace?.path])
  const updateOpenFiles = useCallback((update: (current: Record<string, OpenFile>) => Record<string, OpenFile>) => {
    if (!workspace) return
    setOpenFilesByWorkspace((current) => {
      const nextFiles = update(current[workspace.path] ?? {})
      const paths = Object.keys(nextFiles)
      const boundedFiles = paths.length > MAX_OPEN_FILES_PER_WORKSPACE
        ? Object.fromEntries(paths.slice(-MAX_OPEN_FILES_PER_WORKSPACE).map((path) => [path, nextFiles[path]]))
        : nextFiles
      return { ...current, [workspace.path]: boundedFiles }
    })
  }, [workspace?.path])
  const deferredQuery = useDeferredValue(query)
  const files = useMemo(() => filterFiles(workspaceFiles, deferredQuery), [workspaceFiles, deferredQuery])
  const tabs = useMemo<Tab[]>(() => [
    ...sessions.map((session) => ({ id: session.id, label: session.name, kind: 'session' as const })),
    ...Object.values(openFiles).map((file) => ({ id: `file:${file.path}`, label: file.path.split(/[\\/]/).pop() ?? file.path, kind: 'file' as const })),
  ], [openFiles, sessions])
  const changedEntries = useMemo(() => Object.entries(git.entries), [git.entries])

  const applyWorkspaceSnapshot = (snapshot: WorkspaceSnapshot) => {
    setWorkspaceFiles(buildFileTree(snapshot.files, snapshot.directories))
    setGit(snapshot.git)
  }

  const refreshWorkspace = async (force = false) => {
    if (!workspace) return
    const workspacePath = workspace.path
    setWorkspaceRefreshing(true)
    try {
      const cached = workspaceSnapshotCache.current[workspacePath]
      if (cached) {
        applyWorkspaceSnapshot(cached.snapshot)
        if (!force && Date.now() - cached.refreshedAt < 5_000) return
      }
      const snapshot = await getWorkspaceSnapshot(workspacePath)
      workspaceSnapshotCache.current[workspacePath] = { snapshot, refreshedAt: Date.now() }
      const cachedPaths = Object.keys(workspaceSnapshotCache.current)
      if (cachedPaths.length > MAX_SNAPSHOT_CACHE_ENTRIES) {
        delete workspaceSnapshotCache.current[cachedPaths[0]]
      }
      if (currentWorkspacePath.current === workspacePath) {
        applyWorkspaceSnapshot(snapshot)
        showNotice(snapshot.rollbackWarning ?? '파일 목록과 Git 상태를 새로고침했습니다.')
      }
    } catch (error) {
      if (currentWorkspacePath.current === workspacePath) showNotice(`새로고침 실패: ${String(error)}`)
    } finally {
      if (currentWorkspacePath.current === workspacePath) setWorkspaceRefreshing(false)
    }
  }

  const applyUsageSnapshot = (snapshot: UsageSnapshot) => {
    const primary = snapshot.rateLimits?.primary ?? snapshot.primary
    const percent = Number(primary?.usedPercent)
    const credits = snapshot.rateLimitResetCredits?.credits ?? []
    setUsage({
      percent: Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : null,
      resetsAt: primary?.resetsAt,
      credits: snapshot.rateLimitResetCredits?.availableCount ?? credits.length,
      creditId: credits[0]?.id ?? '',
    })
  }

  const refreshUsage = async (force = false) => {
    if (!workspace) return
    const workspacePath = workspace.path
    const cached = usageCache.current[workspacePath]
    if (cached) {
      if (currentWorkspacePath.current === workspacePath) applyUsageSnapshot(cached.snapshot)
      if (!force && Date.now() - cached.refreshedAt < 30_000) return
    }
    try {
      const snapshot = await getUsageSnapshot(workspacePath)
      usageCache.current[workspacePath] = { snapshot, refreshedAt: Date.now() }
      if (currentWorkspacePath.current === workspacePath) applyUsageSnapshot(snapshot)
    } catch {
      if (currentWorkspacePath.current === workspacePath) setUsage({ percent: null, credits: 0, creditId: '' })
    }
  }

  const consumeUsageReset = async () => {
    if (!workspace || !usage.credits || usageResetting) return
    if (!window.confirm(`사용량 초기화권 ${usage.credits}개 중 1개를 사용하시겠습니까?`)) return
    setUsageResetting(true)
    const resetKey = JSON.stringify([workspace.path, usage.creditId])
    const storedResetKey = `personal-agent:usage-reset-request:${resetKey}`
    let idempotencyKey: string | undefined = usageResetKeys.current[resetKey] || undefined
    if (!idempotencyKey) {
      try { idempotencyKey = window.localStorage.getItem(storedResetKey) ?? undefined } catch { /* storage may be unavailable */ }
    }
    const requestKey = idempotencyKey ?? crypto.randomUUID()
    usageResetKeys.current[resetKey] = requestKey
    try { window.localStorage.setItem(storedResetKey, requestKey) } catch { /* in-memory retry remains available */ }
    try {
      await resetUsage(workspace.path, usage.creditId, requestKey)
      delete usageResetKeys.current[resetKey]
      try { window.localStorage.removeItem(storedResetKey) } catch { /* leave cleanup for a later run */ }
      await refreshUsage(true)
      showNotice('사용량 초기화권을 사용했습니다.')
    } catch (error) {
      showNotice(`사용량 초기화 실패: ${String(error)}. 다시 시도하면 같은 요청으로 처리됩니다.`)
    } finally {
      setUsageResetting(false)
    }
  }

  const installUpdate = async () => {
    if (!availableUpdate || updateInstalling) return
    setUpdateInstalling(true)
    setUpdateError('')
    setUpdateProgress(0)
    try {
      await availableUpdate.downloadAndInstall((event) => {
        if (event.event === 'Started') setUpdateProgress(1)
        if (event.event === 'Progress') setUpdateProgress((current) => current === null ? 1 : Math.min(95, current + 1))
        if (event.event === 'Finished') setUpdateProgress(100)
      })
      await relaunch()
    } catch (error) {
      setUpdateError(`업데이트 설치 실패: ${String(error)}`)
      setUpdateInstalling(false)
      setUpdateProgress(null)
    }
  }

  useEffect(() => { if (workspace) saveSessions(workspace.path, sessions, window.localStorage) }, [workspace?.path, sessions])
  useEffect(() => {
    const isDevelopment = (import.meta as ImportMeta & { env?: { DEV?: boolean } }).env?.DEV
    if (isDevelopment || !('__TAURI_INTERNALS__' in window)) return
    let cancelled = false
    let checking = false
    const checkForUpdates = async () => {
      if (cancelled || checking) return
      checking = true
      setUpdateChecking(true)
      try {
        const [version, update] = await Promise.all([getVersion(), check()])
        if (cancelled) return
        setCurrentVersion(version)
        setAvailableUpdate(update)
        setUpdateError('')
      } catch (error) {
        if (!cancelled) {
          setUpdateError(`업데이트 확인 실패: ${String(error)}`)
          console.error('업데이트 확인 실패', error)
        }
      } finally {
        checking = false
        if (!cancelled) setUpdateChecking(false)
      }
    }
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') void checkForUpdates()
    }
    const handleManualCheck = () => void checkForUpdates()
    void checkForUpdates()
    const intervalId = window.setInterval(() => void checkForUpdates(), UPDATE_CHECK_INTERVAL_MS)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    window.addEventListener('personal-agent:check-updates', handleManualCheck)
    return () => {
      cancelled = true
      window.clearInterval(intervalId)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      window.removeEventListener('personal-agent:check-updates', handleManualCheck)
    }
  }, [])
  useEffect(() => { if (workspace) setSessionsByWorkspace((current) => ({ ...current, [workspace.path]: sessions })) }, [workspace?.path, sessions])
  useEffect(() => { window.localStorage.setItem('personal-agent:workspaces', JSON.stringify(workspaces)) }, [workspaces])
  useEffect(() => {
    const closeMenu = () => setWorkspaceMenu(null)
    window.addEventListener('click', closeMenu)
    return () => window.removeEventListener('click', closeMenu)
  }, [])
  useEffect(() => { if (workspace && activeSessionId) window.localStorage.setItem(`personal-agent:active-session:${workspace.path}`, activeSessionId) }, [workspace?.path, activeSessionId])
  useEffect(() => {
    if (!workspace) {
      setWorkspaceFiles([])
      setGit({ available: false, branch: '', entries: {} })
      setUsage({ percent: null, credits: 0, creditId: '' })
      return
    }
    const storedActive = window.localStorage.getItem(`personal-agent:active-session:${workspace.path}`)
    if (storedActive && sessions.some((session) => session.id === storedActive)) { setActiveSessionId(storedActive); updateActiveTab(storedActive) }
    void refreshWorkspace()
    void refreshUsage()
  }, [workspace?.path])

  const openFile = useCallback(async (path: string) => {
    if (!workspace) return
    const id = `file:${path}`
    const existing = openFiles[path]
    if (existing?.kind === 'file' && existing.content !== undefined && !existing.loading) {
      updateActiveTab(id)
      return
    }
    updateOpenFiles((current) => ({ ...current, [path]: { path, loading: true, kind: 'file' } }))
    updateActiveTab(id)
    try {
      const file = await readFile(workspace.path, path)
      updateOpenFiles((current) => ({ ...current, [path]: { ...file, loading: false, kind: 'file' } }))
    } catch (error) {
      updateOpenFiles((current) => ({ ...current, [path]: { path, loading: false, error: String(error), kind: 'file' } }))
    }
  }, [updateActiveTab, updateOpenFiles, workspace?.path])

  const openChangedFile = async (path: string) => {
    if (!workspace) return
    const id = `file:${path}`
    const existing = openFiles[path]
    if (existing?.kind === 'diff' && existing.content !== undefined && !existing.loading) {
      updateActiveTab(id)
      return
    }
    updateOpenFiles((current) => ({ ...current, [path]: { path, loading: true, kind: 'diff' } }))
    updateActiveTab(id)
    try {
      const file = await readDiff(workspace.path, path)
      updateOpenFiles((current) => ({ ...current, [path]: { ...file, loading: false, kind: 'diff' } }))
    } catch (error) {
      updateOpenFiles((current) => ({ ...current, [path]: { path, loading: false, error: String(error), kind: 'diff' } }))
    }
  }

  const closeTab = (tab: Tab) => {
    if (tab.kind === 'file') {
      const path = tab.id.slice('file:'.length)
      updateOpenFiles((current) => { const next = { ...current }; delete next[path]; return next })
      if (activeTab === tab.id) updateActiveTab(activeSessionId)
      return
    }
    if (sessions.length === 1) return
    if (workspace) void stopTerminal(`${workspace.path}::${tab.id}`)
    const nextSessions = sessions.filter((session) => session.id !== tab.id)
    setSessions(nextSessions)
    if (activeSessionId === tab.id) { setActiveSessionId(nextSessions[0].id); updateActiveTab(nextSessions[0].id) }
  }

  const createSession = () => {
    if (!workspace) return
    const number = sessions.length + 1
    const session = { id: `session-${Date.now()}`, name: `세션 ${number}` }
    setSessions((current) => [...current, session]); setActiveSessionId(session.id); updateActiveTab(session.id)
  }

  const selectTab = (tab: Tab) => { updateActiveTab(tab.id); if (tab.kind === 'session') setActiveSessionId(tab.id) }
  const activeFile = activeTab.startsWith('file:') ? openFiles[activeTab.slice('file:'.length)] : undefined
  const approveChangedFile = async (path: string) => { if (!workspace) return; try { await approveFile(workspace.path, path); await refreshWorkspace(true); showNotice(`${path} 변경을 승인했습니다.`) } catch (error) { showNotice(`변경 승인 실패: ${String(error)}`) } }
  const rollbackChangedFile = async (path: string) => {
    if (!workspace) return
    if (!window.confirm(`${path} 파일을 기준 상태로 되돌릴까요? 새 파일은 복구 보관함으로 이동합니다.`)) return
    try {
      const result = await rollbackFile(workspace.path, path)
      await refreshWorkspace(true)
      showNotice(result.recoveryLocation
        ? `${path} 변경을 되돌렸습니다. 이전 내용 보관 위치: ${result.recoveryLocation}`
        : `${path} 변경을 되돌렸습니다.`)
    } catch (error) { showNotice(`되돌리기 실패: ${String(error)}`) }
  }

  const addWorkspace = async () => {
    if (!('__TAURI_INTERNALS__' in window)) { window.alert('작업 공간 추가는 Tauri 데스크톱 앱에서 사용할 수 있습니다.'); return }
    const selected = await open({ directory: true, multiple: false, title: '작업 공간 선택' })
    if (typeof selected !== 'string') return
    const name = selected.split(/[\\/]/).filter(Boolean).pop() ?? selected
    const nextWorkspace = { id: selected, name, path: selected, branch: '' }
    const existing = workspaces.find((item) => item.path.toLowerCase() === selected.toLowerCase())
    if (existing) {
      switchWorkspace(existing)
      return
    }
    setWorkspaces((current) => [...current, nextWorkspace])
    switchWorkspace(nextWorkspace)
  }

  const switchWorkspace = (nextWorkspace: Workspace) => {
    setWorkspace(nextWorkspace)
    setQuery('')
    const nextSessions = loadSessions(nextWorkspace.path, window.localStorage)
    setSessions(nextSessions)
    const nextTab = activeTabs[nextWorkspace.path] ?? nextSessions[0]?.id ?? ''
    setActiveSessionId(nextSessions.some((session) => session.id === nextTab) ? nextTab : nextSessions[0]?.id ?? '')
    setActiveTabs((current) => ({ ...current, [nextWorkspace.path]: nextTab }))
  }

  const openCommandPalette = useCallback(() => {
    setCommandPaletteOpen(true)
    setPaletteQuery('')
    setPaletteIndex(0)
    window.requestAnimationFrame(() => paletteInputRef.current?.focus())
  }, [])

  const closeCommandPalette = useCallback(() => {
    setCommandPaletteOpen(false)
    setPaletteQuery('')
    setPaletteIndex(0)
  }, [])

  const paletteItems = useMemo<PaletteItem[]>(() => {
    const items: PaletteItem[] = workspaces.map((item) => ({
      id: `workspace:${item.path}`,
      label: item.name,
      detail: item.path,
      kind: 'workspace',
      action: () => switchWorkspace(item),
    }))
    const allSessions = workspaces.flatMap((item) => (item.path === workspace?.path ? sessions : sessionsByWorkspace[item.path] ?? []).map((session) => ({ item, session })))
    allSessions.forEach(({ item, session }) => items.push({
      id: `session:${item.path}:${session.id}`,
      label: session.name,
      detail: `${item.name} · 세션`,
      kind: 'session',
      action: () => { if (item.path !== workspace?.path) switchWorkspace(item); setActiveSessionId(session.id); setActiveTabs((current) => ({ ...current, [item.path]: session.id })) },
    }))
    if (workspace) flattenFilePaths(workspaceFiles).forEach((path) => items.push({
      id: `file:${workspace.path}:${path}`,
      label: path,
      detail: `${workspace.name} · 파일`,
      kind: 'file',
      action: () => void openFile(path),
    }))
    const normalized = paletteQuery.trim().toLowerCase()
    return normalized ? items.filter((item) => `${item.label} ${item.detail}`.toLowerCase().includes(normalized)) : items.slice(0, 40)
  }, [openFile, paletteQuery, sessions, sessionsByWorkspace, switchWorkspace, workspace, workspaceFiles, workspaces])

  const choosePaletteItem = (item?: PaletteItem) => {
    if (!item) return
    item.action()
    closeCommandPalette()
  }

  useEffect(() => {
    if (!commandPaletteOpen) return
    setPaletteIndex((current) => Math.min(current, Math.max(0, paletteItems.length - 1)))
  }, [commandPaletteOpen, paletteItems.length])

  useEffect(() => {
    const handleKeyboard = (event: KeyboardEvent) => {
      const modifier = event.metaKey || event.ctrlKey
      if (modifier && event.key.toLowerCase() === 'p') {
        event.preventDefault()
        openCommandPalette()
        return
      }
      if (modifier && event.shiftKey && event.key.toLowerCase() === 'f') {
        event.preventDefault()
        fileSearchRef.current?.focus()
        return
      }
      if (modifier && event.shiftKey && event.key.toLowerCase() === 'n') {
        event.preventDefault()
        createSession()
        return
      }
      if (event.key === 'Escape') {
        if (commandPaletteOpen) closeCommandPalette()
        else if (workspaceMenu) setWorkspaceMenu(null)
        else if (usageOpen) setUsageOpen(false)
      }
    }
    window.addEventListener('keydown', handleKeyboard)
    return () => window.removeEventListener('keydown', handleKeyboard)
  }, [commandPaletteOpen, createSession, closeCommandPalette, openCommandPalette, usageOpen, workspaceMenu])

  const removeWorkspace = (path: string) => {
    const target = workspaces.find((item) => item.path === path)
    if (!target || !window.confirm(`프로젝트 목록에서 '${target.name}'을 제거할까요?\n실제 폴더와 파일은 삭제되지 않습니다.`)) return
    const removedSessions = sessionsByWorkspace[path] ?? (workspace?.path === path ? sessions : [])
    removedSessions.forEach((session) => { void stopTerminal(`${path}::${session.id}`) })
    const remaining = workspaces.filter((item) => item.path !== path)
    setWorkspaces(remaining)
    setWorkspaceMenu(null)
    if (workspace?.path !== path) return
    if (remaining[0]) {
      switchWorkspace(remaining[0])
      return
    }
    setWorkspace(null)
    setSessions([])
    setActiveSessionId('')
    setWorkspaceFiles([])
    setGit({ available: false, branch: '', entries: {} })
    setUsage({ percent: null, credits: 0, creditId: '' })
    setUsageOpen(false)
  }

  return <main className={`app-shell ${filePanelOpen ? '' : 'file-panel-closed'}`} style={{ '--sidebar-width': `${sidebarWidth}px`, '--file-panel-width': `${filePanelWidth}px` } as CSSProperties}>
    <header className="mobile-header"><button className="icon-button" onClick={() => setSidebarOpen((open) => !open)} aria-label="프로젝트 사이드바 열기"><Menu size={18} /></button><span className="mobile-brand">PERSONAL AGENT</span><button className="icon-button" onClick={() => setFilePanelOpen((open) => !open)} aria-label="파일 패널 열기"><PanelLeft size={18} /></button></header>
    <aside className={`project-sidebar ${sidebarOpen ? '' : 'is-collapsed'}`}>
      <div className="brand-block"><div className="brand-mark"><TerminalSquare size={17} /></div><div><strong>PERSONAL AGENT</strong><span>LOCAL CODING WORKBENCH</span></div></div>
      <div className="section-label">프로젝트 <button className="tiny-button" onClick={() => void addWorkspace()} aria-label="작업 공간 추가" title="작업 공간 추가"><Plus size={15} /></button></div>
      <div className="workspace-list">{workspaces.length ? workspaces.map((item) => <div className="workspace-entry" key={item.path}><button className={`workspace-card ${item.path === workspace?.path ? 'is-active' : ''}`} onClick={() => switchWorkspace(item)} onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); setWorkspaceMenu({ path: item.path, x: event.clientX, y: event.clientY }) }} title={item.path}><div className="workspace-icon"><SquareTerminal size={16} /></div><div className="workspace-copy"><strong>{item.name}</strong><span>{item.path}</span></div>{item.path === workspace?.path && <span className="workspace-dot" />}</button><button className="workspace-more" onClick={(event) => { event.stopPropagation(); const rect = event.currentTarget.getBoundingClientRect(); setWorkspaceMenu({ path: item.path, x: Math.max(8, rect.right - 164), y: rect.bottom + 4 }) }} aria-label={`${item.name} 작업 공간 메뉴`} title="작업 공간 메뉴"><MoreHorizontal size={16} /></button></div>) : <div className="workspace-empty"><FolderPlus size={15} /><span>프로젝트를 추가하세요</span></div>}</div>
      {workspaceMenu && <div className="workspace-context-menu" style={{ left: workspaceMenu.x, top: workspaceMenu.y }} onClick={(event) => event.stopPropagation()}><button onClick={() => removeWorkspace(workspaceMenu.path)}>작업 공간 제거</button></div>}
      <div className="sidebar-spacer" />
    </aside>
    <div className="resize-handle" role="separator" aria-orientation="vertical" aria-label="왼쪽 영역 너비 조절" onPointerDown={(event) => startResize('left', event)} />
    <section className="workspace-main">
      {availableUpdate && <div className="update-banner" role="status"><div><strong>새 버전이 있습니다</strong><span>현재 v{currentVersion || '—'} → 새 버전 v{availableUpdate.version}</span>{updateError && <em>{updateError}</em>}</div><button onClick={() => void installUpdate()} disabled={updateInstalling}>{updateInstalling ? `업데이트 중${updateProgress === null ? '…' : ` ${updateProgress}%`}` : '업데이트'}</button></div>}
      <div className="workspace-toolbar"><div className="workspace-context"><span className="eyebrow">CURRENT WORKSPACE</span><strong>{workspace?.name || '작업 공간을 선택하세요'}</strong><span title={workspace?.path}>{workspace?.path || '프로젝트를 추가하면 터미널이 시작됩니다.'}</span></div><div className="toolbar-actions"><button className="toolbar-button" onClick={openCommandPalette} aria-label="빠른 이동 열기" title="빠른 이동 (⌘P / Ctrl+P)"><Command size={14} /><span>빠른 이동</span><kbd>⌘P</kbd></button><button className="toolbar-button" onClick={() => void refreshWorkspace(true)} disabled={!workspace || workspaceRefreshing} aria-label="파일 목록 새로고침" title="파일 목록 새로고침"><RefreshCw className={workspaceRefreshing ? 'spin' : ''} size={14} /><span>{workspaceRefreshing ? '새로고침 중' : '새로고침'}</span></button></div></div>
      <div className="tab-bar"><div className="tabs" role="tablist" aria-label="열린 세션 및 파일">{tabs.map((tab) => <div key={tab.id} className={`tab ${activeTab === tab.id ? 'is-active' : ''}`} onClick={() => selectTab(tab)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectTab(tab) } }} role="tab" tabIndex={0} aria-selected={activeTab === tab.id} title={tab.label}>{tab.kind === 'session' ? <SquareTerminal size={14} /> : <FileCode2 size={14} />}<span>{tab.label}</span>{(tab.kind === 'file' || sessions.length > 1) && <button className="tab-close" onClick={(event) => { event.stopPropagation(); closeTab(tab) }} aria-label={`${tab.label} 닫기`} title="닫기"><X size={13} /></button>}</div>)}</div>{!filePanelOpen && <button className="file-panel-toggle" onClick={() => setFilePanelOpen(true)} aria-label="프로젝트 파일 패널 열기" title="프로젝트 파일 패널 열기"><PanelLeft size={16} /></button>}<button className="new-session-button" onClick={createSession} disabled={!workspace} aria-label="새 Codex 세션" title="새 세션 (Ctrl+Shift+N)"><Plus size={17} /></button></div>
      <div className={`terminal-view ${activeFile ? 'is-file-view' : ''}`} role="log" aria-label="터미널">
        {workspace ? <Suspense fallback={<div className="terminal-loading">터미널 준비 중…</div>}>{workspaces.map((item) => {
          const itemSessions = item.path === workspace.path ? sessions : sessionsByWorkspace[item.path] ?? []
          return <div key={item.path} className={`workspace-terminal-group ${item.path === workspace.path ? 'is-active' : ''} ${activeFile ? 'is-file-hidden' : ''}`} aria-hidden={item.path !== workspace.path || Boolean(activeFile)}>
            {itemSessions.map((session) => <div key={`${item.path}:${session.id}`} className={`terminal-session ${!activeFile && item.path === workspace.path && session.id === activeSessionId ? 'is-visible' : ''}`}><TerminalPane sessionId={session.id} workspace={item.path} /></div>)}
          </div>
        })}</Suspense> : <div className="empty-workspace"><FolderPlus size={28} /><h1>작업 공간이 없습니다</h1><p>프로젝트를 추가하면 터미널과 파일 탐색기가 시작됩니다.</p><button onClick={() => void addWorkspace()}><Plus size={15} /> 작업 공간 추가</button></div>}
        {activeFile && <FileView file={activeFile} />}
      </div>
    </section>
    <div className={`resize-handle ${filePanelOpen ? '' : 'is-disabled'}`} role="separator" aria-orientation="vertical" aria-label="오른쪽 영역 너비 조절" onPointerDown={(event) => { if (filePanelOpen) startResize('right', event) }} />
    <aside className={`file-panel ${filePanelOpen ? '' : 'is-collapsed'}`}>
      {!workspace ? <div className="file-panel-empty"><FolderPlus size={22} /><strong>프로젝트 파일</strong><span>작업 공간을 추가하면 파일이 표시됩니다.</span></div> : <>
      <div className="file-panel-header"><div><span className="eyebrow">WORKSPACE</span><h2>프로젝트 파일</h2></div><button className="icon-button" onClick={() => setFilePanelOpen(false)} aria-label="파일 패널 닫기"><PanelLeft size={16} /></button></div>
      <div className="git-summary"><GitBranch size={14} /><span>{git.branch || workspace.branch || '브랜치 없음'}</span><span className="separator">·</span><span className="changed-count">{changedEntries.length} 변경</span></div>
      <label className="search-field"><Search size={15} /><input ref={fileSearchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="파일 검색…" aria-label="파일 검색" /><kbd>⇧⌘F</kbd>{query && <button className="clear-search" onClick={() => setQuery('')} aria-label="파일 검색 지우기" title="검색 지우기"><X size={13} /></button>}</label>
      {files.length ? <FileTree entries={files} onOpen={openFile} /> : <div className="file-tree-empty"><Search size={18} /><span>일치하는 파일 없음</span><button onClick={() => setQuery('')}>검색 지우기</button></div>}
      {changedEntries.length > 0 && <div className="changed-files" aria-live="polite"><span className="eyebrow">CHANGED FILES</span>{changedEntries.slice(0, 5).map(([path, status]) => <div key={path} className="changed-file"><button className="changed-file-name" onClick={() => void openChangedFile(path)}><span>{status.trim() || 'M'}</span>{path}</button><button className="change-action approve" onClick={() => void approveChangedFile(path)} aria-label={`${path} 변경 승인`}>승인</button><button className="change-action rollback" onClick={() => void rollbackChangedFile(path)} aria-label={`${path} 변경 되돌리기`}>되돌리기</button></div>)}</div>}
      </>}
    </aside>
    <div className="usage-area">
      {usageOpen && <div className="usage-popover" role="dialog" aria-label="Codex 사용량 상세"><div className="usage-popover-header"><strong>Codex 사용량</strong><button onClick={() => setUsageOpen(false)} aria-label="사용량 상세 닫기"><X size={14} /></button></div><div className="usage-popover-value">{usage.percent === null ? '확인할 수 없음' : `${usage.percent.toFixed(0)}% 사용 중`}</div><div className="usage-popover-meta">{usage.resetsAt ? `${new Date(usage.resetsAt * 1000).toLocaleString()} 재설정` : '재설정 일정 정보 없음'}</div><button className="usage-reset-button" disabled={!usage.credits || usageResetting} onClick={() => void consumeUsageReset()}>{usageResetting ? '초기화 중…' : usage.credits ? `사용량 초기화권 사용 (${usage.credits}개)` : '사용 가능한 초기화권 없음'}</button></div>}
      <div className="status-bar-row"><button className="status-bar" onClick={() => setUsageOpen((open) => !open)} aria-expanded={usageOpen} aria-label="Codex 사용량 상세 열기"><span className="status-bar-label">Codex 사용률</span><div className="status-bar-track"><span style={{ width: `${usage.percent ?? 0}%` }} /></div><strong>{usage.percent === null ? '—' : `${usage.percent.toFixed(0)}%`}</strong><span className="status-bar-hint">상세 보기</span></button><button className="status-bar-refresh" onClick={() => void refreshUsage(true)} aria-label="사용률 새로고침" title="사용률 새로고침"><RefreshCw size={13} /></button><span className="app-version" title="현재 설치된 앱 버전">v{currentVersion || '—'}</span><button className="update-check-button" onClick={() => window.dispatchEvent(new Event('personal-agent:check-updates'))} disabled={updateChecking} aria-label="업데이트 확인" title={updateChecking ? '업데이트 확인 중' : '업데이트 확인'}><RefreshCw size={13} /></button></div>
    </div>
    {notice && <div className="notice-toast" role="status" aria-live="polite"><Check size={14} />{notice}</div>}
    {commandPaletteOpen && <div className="palette-backdrop" onMouseDown={closeCommandPalette}><section className="command-palette" role="dialog" aria-modal="true" aria-label="빠른 이동" onMouseDown={(event) => event.stopPropagation()}><div className="palette-search"><Search size={16} /><input ref={paletteInputRef} value={paletteQuery} onChange={(event) => setPaletteQuery(event.target.value)} onKeyDown={(event: ReactKeyboardEvent<HTMLInputElement>) => { if (event.key === 'ArrowDown') { event.preventDefault(); setPaletteIndex((current) => Math.min(current + 1, Math.max(0, paletteItems.length - 1))) } else if (event.key === 'ArrowUp') { event.preventDefault(); setPaletteIndex((current) => Math.max(0, current - 1)) } else if (event.key === 'Enter') { event.preventDefault(); choosePaletteItem(paletteItems[paletteIndex]) } else if (event.key === 'Escape') { event.preventDefault(); closeCommandPalette() } }} placeholder="작업 공간, 세션, 파일 검색…" aria-label="빠른 이동 검색" /><kbd>ESC</kbd></div><div className="palette-results" role="listbox" aria-label="검색 결과">{paletteItems.length ? paletteItems.map((item, index) => <button key={item.id} className={`palette-item ${index === paletteIndex ? 'is-selected' : ''}`} onMouseEnter={() => setPaletteIndex(index)} onClick={() => choosePaletteItem(item)} role="option" aria-selected={index === paletteIndex}>{item.kind === 'workspace' ? <Folder size={15} /> : item.kind === 'session' ? <SquareTerminal size={15} /> : <FileCode2 size={15} />}<span><strong>{item.label}</strong><small>{item.detail}</small></span>{index === paletteIndex && <Check size={14} />}</button>) : <div className="palette-empty"><Search size={18} /><span>검색 결과 없음</span></div>}</div><div className="palette-footer"><span><ArrowUp size={12} /><ArrowDown size={12} /> 이동</span><span><Keyboard size={12} /> Enter 선택</span><span>Ctrl+P 다시 열기</span></div></section></div>}
  </main>
}

export default App
