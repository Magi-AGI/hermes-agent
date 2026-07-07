'use strict'

const fs = require('node:fs')
const path = require('node:path')

const REGISTRY_SCHEMA_VERSION = 1
const BACKEND_OWNER_ARG = '--hermes-desktop-owner'
const SECRET_KEY_RE = /(token|secret|api[_-]?key|authorization|bearer|password|credential)/i
// OS create-time is read at two moments by two psutil callers (the child that
// writes the sidecar and the parent that reconciles the live PID). They read
// the same NT/POSIX process-creation instant, so they normally match exactly,
// but float serialization and cross-API rounding on Windows can differ by a
// fraction of a second. Absorb that with a small tolerance; a reused PID gets a
// create-time seconds-to-minutes later, so this cannot mask PID reuse —
// especially combined with the owner-nonce and install-root gates.
const PROCESS_START_TIME_TOLERANCE_SECONDS = 2

// Coerce a create-time to epoch SECONDS. psutil emits float seconds (~1.7e9);
// a source that emits epoch milliseconds (~1.7e12) is divided down so both
// sides compare on one scale. Non-finite/missing values normalize to null.
function normalizeProcessStartTime(value) {
  if (value === undefined || value === null) return null
  const num = Number(value)
  if (!Number.isFinite(num)) return null
  return num > 1e12 ? num / 1000 : num
}

function normalizeHermesRoot(hermesHome, { pathModule = path } = {}) {
  if (!hermesHome) return hermesHome
  const resolved = pathModule.resolve(String(hermesHome))
  const parent = pathModule.dirname(resolved)
  if (pathModule.basename(parent).toLowerCase() === 'profiles') {
    return pathModule.dirname(parent)
  }
  return resolved
}

function desktopBackendRegistryPath(hermesHome, { pathModule = path } = {}) {
  return pathModule.join(normalizeHermesRoot(hermesHome, { pathModule }), 'runtime', 'desktop-backends.json')
}

function backendDescriptorId(descriptor) {
  const scope = descriptor?.scope
  const profile = String(descriptor?.profile || '').trim()
  if (!profile) throw new Error('backend descriptor requires profile')
  if (scope === 'profile') return `profile:${profile}`
  if (scope === 'session') {
    const sessionId = String(descriptor?.sessionId || '').trim()
    if (!sessionId) throw new Error('session backend descriptor requires sessionId')
    return `session:${profile}:${sessionId}`
  }
  throw new Error(`unknown backend scope: ${scope}`)
}

function backendOwnerArg(ownerNonce) {
  return `${BACKEND_OWNER_ARG}=${String(ownerNonce || '')}`
}

function hasSecretLikeRegistryField(value) {
  const seen = new Set()
  function visit(node) {
    if (!node || typeof node !== 'object') return false
    if (seen.has(node)) return false
    seen.add(node)
    for (const [key, child] of Object.entries(node)) {
      if (SECRET_KEY_RE.test(key)) return true
      if (visit(child)) return true
    }
    return false
  }
  return visit(value)
}

function registryRecordForDescriptor(descriptor, options = {}) {
  const scope = descriptor?.scope
  const profile = String(descriptor?.profile || '').trim()
  const sessionId = descriptor?.sessionId ? String(descriptor.sessionId) : null
  const id = descriptor.id || backendDescriptorId({ scope, profile, sessionId })
  const lifecycle = descriptor.lifecycle || 'starting'
  return {
    schemaVersion: REGISTRY_SCHEMA_VERSION,
    id,
    scope,
    profile,
    ...(scope === 'session' ? { sessionId } : {}),
    pid: Number.isInteger(descriptor?.pid) ? descriptor.pid : null,
    processStartTime: descriptor?.processStartTime ?? null,
    installRoot: descriptor?.installRoot || null,
    startedAt: options.startedAt || descriptor?.startedAt || new Date().toISOString(),
    lastActiveAt: descriptor?.lastActiveAt || Date.now(),
    lifecycle,
    startedBy: 'hermes-desktop',
    desktopInstanceId: options.desktopInstanceId || descriptor?.desktopInstanceId || null,
    ownerNonce: options.ownerNonce || descriptor?.ownerNonce || null,
    sidecarPath: options.sidecarPath || descriptor?.sidecarPath || null
  }
}

function readBackendRegistry(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'))
    if (Array.isArray(parsed)) return { records: parsed, corrupt: false }
    if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.backends)) {
      return { records: [], corrupt: true }
    }
    return { records: parsed.backends, corrupt: false }
  } catch {
    return { records: [], corrupt: true }
  }
}

function writeBackendRegistry(file, records) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const payload = {
    schemaVersion: REGISTRY_SCHEMA_VERSION,
    updatedAt: new Date().toISOString(),
    backends: Array.isArray(records) ? records : []
  }
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`
  fs.writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
  fs.renameSync(tmp, file)
}

function argvHasOwnerNonce(argv, ownerNonce) {
  if (!Array.isArray(argv) || !ownerNonce) return false
  const expectedEquals = backendOwnerArg(ownerNonce)
  const expectedSeparateIndex = argv.findIndex(arg => arg === BACKEND_OWNER_ARG)
  return argv.includes(expectedEquals) || (expectedSeparateIndex >= 0 && argv[expectedSeparateIndex + 1] === ownerNonce)
}

function sidecarHasOwnerNonce(sidecars, sidecarPath, ownerNonce) {
  if (!sidecarPath || !ownerNonce) return false
  const sidecar = sidecars?.[sidecarPath]
  return Boolean(sidecar && sidecar.kind === 'hermes-desktop-backend' && sidecar.ownerNonce === ownerNonce)
}

// True iff a freshly-read sidecar object is OUR generation's: correct kind AND an
// exact owner-nonce match. Never adopts a foreign/prior-generation sidecar.
function sidecarMatchesOwner(sidecar, ownerNonce) {
  return Boolean(
    sidecar &&
    ownerNonce &&
    sidecar.kind === 'hermes-desktop-backend' &&
    sidecar.ownerNonce === ownerNonce
  )
}

// Bounded, ownership-safe read of the CURRENT generation's sidecar. The child
// publishes its sidecar just after binding its port, which can race Electron's
// ready-file/stdout readiness signal — so a single read right after "ready" may
// find the file absent (or, on the legacy shared path, a stale prior-generation
// file). This polls `read()` up to `attempts` times, ONLY accepting a sidecar
// whose ownerNonce matches this spawn's nonce, and returns:
//   { sidecar, verified: true }  once a matching sidecar is observed
//   { sidecar, verified: false } otherwise — `sidecar` is the last non-matching
//                                file seen (for logging), or null if none.
// Pure/injectable (read + sleep passed in) so it is unit-testable without disk.
async function readOwnedSidecarWithRetry({ read, ownerNonce, attempts = 5, delayMs = 100, sleep } = {}) {
  const wait = sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)))
  const total = Math.max(1, attempts | 0)
  let last = null
  for (let i = 0; i < total; i++) {
    let current = null
    try {
      current = read()
    } catch {
      current = null
    }
    if (sidecarMatchesOwner(current, ownerNonce)) {
      return { sidecar: current, verified: true }
    }
    if (current) last = current
    if (i < total - 1) await wait(delayMs)
  }
  return { sidecar: last, verified: false }
}

function verifyBackendOwnership(record, proc, options = {}) {
  if (!record || !proc) return { killable: false, reason: 'missing_record_or_process' }
  if (hasSecretLikeRegistryField(record)) return { killable: false, reason: 'registry_contains_secret_like_field' }
  if (proc.cmdlineReadable === false) return { killable: false, reason: 'cmdline_unreadable' }
  if (String(record.pid) !== String(proc.pid)) return { killable: false, reason: 'pid_mismatch' }
  const recordStart = normalizeProcessStartTime(record.processStartTime ?? record.createTime)
  const procStart = normalizeProcessStartTime(proc.processStartTime ?? proc.createTime)
  if (recordStart !== null && procStart !== null && Math.abs(recordStart - procStart) > PROCESS_START_TIME_TOLERANCE_SECONDS) {
    return { killable: false, reason: 'pid_reuse_start_time_mismatch' }
  }
  const expectedInstallRoot = options.expectedInstallRoot || record.installRoot
  if (!expectedInstallRoot || record.installRoot !== expectedInstallRoot || proc.installRoot !== expectedInstallRoot) {
    return { killable: false, reason: 'install_root_mismatch' }
  }
  const ownerNonce = record.ownerNonce
  if (!ownerNonce) return { killable: false, reason: 'missing_observable_nonce' }
  if (argvHasOwnerNonce(proc.argv, ownerNonce)) return { killable: true, reason: 'argv_nonce_verified' }
  if (sidecarHasOwnerNonce(options.sidecars, record.sidecarPath, ownerNonce)) {
    return { killable: true, reason: 'sidecar_nonce_verified' }
  }
  return { killable: false, reason: 'missing_observable_nonce' }
}

const _UNHEALTHY_LIFECYCLES = ['unresponsive', 'failed', 'hung']

function reconcileBackendRegistry(records, processTable, options = {}) {
  const claimedIds = options.claimedIds instanceof Set
    ? options.claimedIds
    : new Set(Array.isArray(options.claimedIds) ? options.claimedIds : [])
  const currentInstanceId = options.currentInstanceId || null
  const actions = []
  for (const record of records || []) {
    const proc = processTable?.[String(record.pid)] || processTable?.[record.pid]
    if (!proc) {
      actions.push({ action: 'prune', id: record.id, pid: record.pid, reason: 'dead_pid' })
      continue
    }
    const verdict = verifyBackendOwnership(record, proc, options)
    // Ownership gating runs FIRST and fails closed: a process we cannot prove is
    // a Desktop-owned backend (manual dashboard, wrong install root, unreadable
    // cmdline, missing nonce, PID reuse) is never killed regardless of instance.
    if (!verdict.killable) {
      actions.push({ action: 'keep_fail_closed', id: record.id, pid: record.pid, reason: verdict.reason })
      continue
    }
    const children = proc.children || []
    const recordInstance = record.desktopInstanceId || null
    const foreignInstance = Boolean(currentInstanceId && recordInstance && recordInstance !== currentInstanceId)
    const unhealthy = _UNHEALTHY_LIFECYCLES.includes(record.lifecycle || record.state)
    if (claimedIds.has(record.id)) {
      // A backend the live pool still holds is off-limits to the reaper; the
      // pool's own health-probe/restart owns its lifecycle.
      actions.push({ action: 'keep', id: record.id, pid: record.pid, reason: 'claimed_current_instance' })
    } else if (foreignInstance) {
      // Verified-owned but belongs to a prior Desktop instance (crash/restart)
      // and no live window claims it — this is the orphan reaper's whole point.
      actions.push({ action: 'kill_tree', id: record.id, pid: record.pid, children, reason: 'prior_instance_orphan' })
    } else if (unhealthy) {
      actions.push({ action: 'kill_tree', id: record.id, pid: record.pid, children, reason: verdict.reason })
    } else {
      actions.push({ action: 'keep', id: record.id, pid: record.pid, reason: 'verified_alive' })
    }
  }
  return actions
}

// Turn raw per-process rows (from Win32_Process / ps enumeration) into the
// process-table shape verifyBackendOwnership/reconcileBackendRegistry consume.
// Pure and unit-testable so the fragile OS enumeration in main.cjs stays a thin
// shell. Each row: { pid, ppid, createTime, cmdline, cmdlineReadable }.
//   - installRoot is claimed ONLY when the process command line evidences the
//     expected install root, so a look-alike process elsewhere fails closed.
//   - children are the pids whose ppid points at this pid.
//   - argv is a whitespace split of the command line (enough for the owner-nonce
//     marker, which is a single `--hermes-desktop-owner=<nonce>` / `... <nonce>`
//     token).
function buildProcessTableFromRows(rows, options = {}) {
  const expectedInstallRoot = options.expectedInstallRoot || null
  const list = Array.isArray(rows) ? rows.filter(row => row && row.pid != null) : []
  const childrenByPpid = new Map()
  for (const row of list) {
    const ppid = row.ppid == null ? null : Number(row.ppid)
    if (ppid == null || Number.isNaN(ppid)) continue
    if (!childrenByPpid.has(ppid)) childrenByPpid.set(ppid, [])
    childrenByPpid.get(ppid).push(Number(row.pid))
  }
  const table = {}
  for (const row of list) {
    const pid = Number(row.pid)
    const cmdline = typeof row.cmdline === 'string' ? row.cmdline : ''
    const cmdlineReadable = row.cmdlineReadable !== false && cmdline.length > 0
      ? true
      : Boolean(row.cmdlineReadable)
    const installRoot = expectedInstallRoot && cmdline.includes(expectedInstallRoot)
      ? expectedInstallRoot
      : null
    table[String(pid)] = {
      pid,
      processStartTime: normalizeProcessStartTime(row.createTime ?? row.processStartTime),
      argv: cmdline ? cmdline.split(/\s+/).filter(Boolean) : [],
      cmdline,
      cmdlineReadable,
      installRoot,
      children: childrenByPpid.get(pid) || []
    }
  }
  return table
}

module.exports = {
  BACKEND_OWNER_ARG,
  PROCESS_START_TIME_TOLERANCE_SECONDS,
  backendDescriptorId,
  backendOwnerArg,
  buildProcessTableFromRows,
  desktopBackendRegistryPath,
  hasSecretLikeRegistryField,
  normalizeProcessStartTime,
  readBackendRegistry,
  readOwnedSidecarWithRetry,
  reconcileBackendRegistry,
  sidecarMatchesOwner,
  registryRecordForDescriptor,
  verifyBackendOwnership,
  writeBackendRegistry
}
