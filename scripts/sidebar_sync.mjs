import { lstat, readFile, readdir, rename, unlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export const name = 'codex-sidebar-sync'
export const inject = ['workspaceRegistry', 'sessionQuery']

const requestName = /^[0-9a-f-]{36}\.publish\.json$/
const sessionName = /^session-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

export async function apply(ctx, { directory, workspace }) {
  let closed = false
  let pending
  let reported = false

  async function publish() {
    const names = (await readdir(directory)).filter(name => requestName.test(name)).slice(0, 12)
    if (!names.length) return
    const group = await ctx.workspaceRegistry.resolveByPath(workspace)
    if (!group) throw new Error('Codex collaboration workspace is not registered.')
    for (const name of names) {
      if (closed) return
      const source = join(directory, name)
      try {
        const info = await lstat(source)
        if (!info.isFile() || info.size > 512) continue
        const { sessionId } = JSON.parse(await readFile(source, 'utf8'))
        if (typeof sessionId !== 'string' || !sessionName.test(sessionId)) continue
        const title = (await ctx.sessionQuery.readTitle(sessionId))?.title
        if (!title || closed) continue
        // The registry validates the stored session's cwd and publishes the group update.
        await group.attachSession(sessionId)
        if (!group.sessionIds.includes(sessionId)) continue
        const receipt = join(directory, name.replace('.publish.json', '.ready.json'))
        await writeFile(receipt + '.tmp', JSON.stringify({ sessionId, workspaceId: group.id, title }), 'utf8')
        await rename(receipt + '.tmp', receipt)
        await unlink(source)
      } catch (error) {
        if (!reported) ctx.logger.warn('Codex sidebar sync: %s', String(error))
        reported = true
      }
    }
  }

  function tick() {
    if (closed || pending) return
    pending = publish().then(() => { reported = false }).catch(error => {
      if (!reported) ctx.logger.warn('Codex sidebar sync: %s', String(error))
      reported = true
    }).finally(() => { pending = undefined })
  }

  ctx.effect(() => {
    const timer = setInterval(tick, 650)
    tick()
    return async () => {
      closed = true
      clearInterval(timer)
      await pending
    }
  }, 'codex sidebar sync')
}
