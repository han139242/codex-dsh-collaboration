export const name = 'codex-session-title'
export const inject = ['sessionTitle']

export function apply(ctx, { title }) {
  let applied = false
  ctx.on('session/event', (session, event) => {
    if (applied || event.type !== 'user/message' || event.data.source.kind !== 'user') return
    applied = true
    queueMicrotask(() => {
      if (ctx.sessionTitle.get(session)?.source.kind === 'user') return
      try {
        ctx.sessionTitle.rename(session, title)
      } catch (error) {
        ctx.logger.warn('Codex session title: %s', String(error))
      }
    })
  }, { global: true })
}
