window.__ModuleLoader__.load({
  id: 'codex-dsh-sidebar-sync',
  factory: () => ({
    inject: ['workspaces', 'sessions'],
    apply(ctx) {
      const workspaceId = "__WORKSPACE_ID__"
      ctx.effect(() => {
        const requested = new Set()
        let closed = false
        const refresh = () => {
          const group = ctx.workspaces.list.getSnapshot().items.find(item => item.workspaceId === workspaceId)
          if (!group) return
          const { byId } = ctx.sessions.list.getSnapshot()
          const missing = group.sessionIds.filter(id => !byId[id]?.title && !byId[id]?.blank && !requested.has(id))
          if (!missing.length) return
          for (const id of missing) requested.add(id)
          const update = async () => {
            if (missing.some(id => !byId[id])) await ctx.sessions.refresh()
            if (closed) return
            // Read the projections without opening the conversation or claiming its writer.
            await Promise.all(missing.map(id => ctx.sessions.refreshProjections(id)))
          }
          update().catch(error => {
            for (const id of missing) requested.delete(id)
            ctx.logger.warn('Codex sidebar titles: %s', String(error))
          })
        }
        const stop = ctx.workspaces.list.subscribe(refresh)
        refresh()
        return () => {
          closed = true
          stop()
        }
      }, 'codex sidebar titles')
    },
  }),
})
