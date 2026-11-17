import { lstat, open, opendir, realpath } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { basename, isAbsolute, join, relative, sep } from 'node:path'

export const name = 'codex-scoped-inspection'
export const inject = ['tools']
const toolName = 'codex_inspect'
const testRequest = 'codex_request_test'
const testResult = 'codex_test_results'
const evidenceRequest = 'codex_request_evidence'
const evidenceResult = 'codex_evidence_results'
const blocked = /^(\.git|\.env(?:\..*)?|\.credentials(?:\..*)?|credentials|secrets|xwechat_files|wechat files|session-backup)$/i

function outside(root, path) {
  const suffix = relative(root, path)
  return suffix === '..' || suffix.startsWith('..' + sep) || isAbsolute(suffix)
}

async function resolveTarget(root, parts) {
  let path = root
  if ((await lstat(root)).isSymbolicLink() || outside(root, await realpath(root))) {
    throw new Error('The inspection root changed.')
  }
  for (const part of parts) {
    path = join(path, part)
    const info = await lstat(path)
    if (info.isSymbolicLink() || outside(root, await realpath(path))) {
      throw new Error('Links and paths outside the inspection root are unavailable.')
    }
  }
  return path
}

async function stats(root, target, list, signal) {
  let bytes = 0
  let files = 0
  let skipped = 0
  const rows = []
  const stack = [target]
  const expires = Date.now() + 4000
  let visited = 0
  let partial = false
  const limited = () => Date.now() >= expires || visited >= 25000
  while (stack.length && !limited()) {
    signal?.throwIfAborted()
    const path = stack.pop()
    visited++
    try {
      const info = await lstat(path)
      if (info.isSymbolicLink() || outside(root, await realpath(path))) {
        skipped++
        continue
      }
      if (info.isFile()) {
        bytes += info.size
        files++
      } else if (info.isDirectory() && !(list && path !== target)) {
        for await (const entry of await opendir(path)) {
          if (limited() || stack.length >= 25000) {
            partial = true
            break
          }
          if (entry.isSymbolicLink() || blocked.test(entry.name)) {
            skipped++
          } else {
            stack.push(join(path, entry.name))
          }
        }
      }
      if (list && path !== target) {
        rows.push({ name: basename(path), directory: info.isDirectory(),
                    bytes: info.isFile() ? info.size : null,
                    modified: info.mtime.toISOString() })
      }
    } catch (error) {
      if (error?.name === 'AbortError') throw error
      skipped++
    }
  }
  return {
    bytes, files, skipped, partial: partial || stack.length > 0 || (list && rows.length > 60),
    ...(list ? { entries: rows.sort((a, b) => (b.bytes ?? 0) - (a.bytes ?? 0)).slice(0, 60) } : {})
  }
}

export async function apply(ctx, config) {
  const roots = []
  for (const entry of config.roots ?? []) {
    if (!isAbsolute(entry.path) || blocked.test(basename(entry.path))) {
      throw new Error('Inspection roots must be explicit absolute paths.')
    }
    const path = await realpath(entry.path)
    const info = await lstat(entry.path)
    if (info.isSymbolicLink() || (!info.isFile() && !info.isDirectory()) || (entry.text && !info.isFile())) {
      throw new Error('Inspection roots must be regular files or physical directories.')
    }
    roots.push({ path, text: Boolean(entry.text) })
  }
  const bridge = config.testBridge
  const evidence = config.evidenceBridge
  if (!roots.length && !bridge && !evidence) throw new Error('At least one scoped tool is required.')
  if (config.restrictTools !== false) {
    const allowed = new Set()
    if (roots.length) allowed.add(toolName)
    if (bridge?.mode === 'request') allowed.add(testRequest)
    if (bridge?.mode === 'results') allowed.add(testResult)
    if (evidence?.mode === 'request') allowed.add(evidenceRequest)
    if (evidence?.mode === 'results') allowed.add(evidenceResult)
    ctx.tools.guard(exec => allowed.has(exec.name) ? undefined : 'Only the scoped tools approved for this turn may be used.')
  }
  const textOutput = { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] }

  if (evidence?.mode === 'request') {
    let requested = 0
    ctx.tools.register({
      name: evidenceRequest,
      description: 'Ask Codex to check one missing fact that could change your judgment. Use file for an authorized file, research for a public source, or blocker for a diagnostic check. This registers a question only. If the answer blocks your decision, share what you verified so far and finish the current reply so Codex can return the evidence in this session.',
      parameters: {
        type: 'object',
        properties: {
          kind: { type: 'string', enum: ['file', 'research', 'blocker'] },
          question: { type: 'string' },
          why: { type: 'string' },
        },
        required: ['kind', 'question', 'why'],
        additionalProperties: false,
      },
      output: textOutput,
      execute({ kind, question, why }) {
        question = question.trim()
        why = why.trim()
        if (!question || !why || question.length > 1200 || why.length > 1200 || requested >= 3) {
          throw new Error('Submit up to three focused evidence questions with a reason for each.')
        }
        requested++
        return JSON.stringify({ request_id: randomUUID(), kind, question, why })
      },
    })
  }
  if (evidence?.mode === 'results') {
    ctx.tools.register({
      name: evidenceResult,
      description: 'Read the actual evidence returned by Codex for your requests in this conversation.',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: textOutput,
      execute() { return JSON.stringify(evidence.results) },
    })
  }

  if (bridge?.mode === 'request') {
    let requested = 0
    ctx.tools.register({
      name: testRequest,
      description: 'Ask Codex to verify one concrete test. Describe the input and expected observable outcome. This records a request only; Codex decides how to run it and returns evidence.',
      parameters: {
        type: 'object',
        properties: { title: { type: 'string' }, input: { type: 'string' }, expected: { type: 'string' } },
        required: ['title', 'input', 'expected'],
        additionalProperties: false,
      },
      output: textOutput,
      execute(args) {
        const request = { title: args.title.trim(), input: args.input.trim(), expected: args.expected.trim() }
        if (Object.values(request).some(value => !value || value.length > 1200) || requested >= 3) {
          throw new Error('Use up to three concrete test requests with short, nonempty fields.')
        }
        requested++
        return JSON.stringify({ request_id: randomUUID(), ...request })
      },
    })
  }
  if (bridge?.mode === 'results') {
    ctx.tools.register({
      name: testResult,
      description: 'Read Codex’s recorded results for the tests requested in this conversation.',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: textOutput,
      execute() { return JSON.stringify(bridge.results) },
    })
  }

  if (!roots.length) return
  ctx.tools.register({
    name: toolName,
    description: 'Inspect authorized local evidence. roots lists permitted entries. list returns immediate names and metadata. measure counts bytes and files beneath a path within a bounded traversal. read_text reads only a file expressly marked for text access.',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['roots', 'list', 'measure', 'read_text'] },
        root: { type: 'integer' },
        path: { type: 'string', description: 'Optional path relative to the numbered root.' }
      },
      required: ['action'], additionalProperties: false
    },
    timeoutMs: 10000,
    output: textOutput,
    async execute(args, exec) {
      if (args.action === 'roots') {
        return JSON.stringify(roots.map((root, index) => ({ root: index, ...root })))
      }
      const entry = roots[args.root]
      const parts = args.path ? args.path.split(/[\\/]/) : []
      if (!entry || parts.some(part => !part || part === '.' || part === '..' ||
          /[:\x00-\x1f]|[. ]$/.test(part) || blocked.test(part))) {
        throw new Error('Select a listed root and a safe relative path.')
      }
      const path = await resolveTarget(entry.path, parts)
      if (args.action === 'read_text') {
        if (parts.length || !entry.text) throw new Error('This file has no text-read authorization.')
        const expected = await lstat(path)
        if (!expected.isFile() || expected.isSymbolicLink()) throw new Error('The evidence file changed.')
        const file = await open(path, 'r')
        try {
          const opened = await file.stat()
          if (opened.dev !== expected.dev || opened.ino !== expected.ino) {
            throw new Error('The evidence file changed.')
          }
          const buffer = Buffer.alloc(48 * 1024 + 1)
          const { bytesRead } = await file.read(buffer, 0, buffer.length, 0)
          const text = buffer.subarray(0, Math.min(bytesRead, buffer.length - 1))
          if (text.includes(0)) throw new Error('Binary content is unavailable.')
          return JSON.stringify({ text: text.toString('utf8'), partial: bytesRead === buffer.length })
        } finally {
          await file.close()
        }
      }
      return JSON.stringify(await stats(entry.path, path, args.action === 'list', exec.signal))
    }
  })
}
