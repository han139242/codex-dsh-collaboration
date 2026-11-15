export const name = 'codex-attach-workspace'
export const inject = ['workspaceRegistry']

export async function apply(ctx, { workspace, sessionId }) {
  try {
    const group = await ctx.workspaceRegistry.resolveByPath(workspace)
    if (!group) throw new Error('The requested DSH workspace is not registered.')
    await group.attachSession(sessionId)
    if (!group.sessionIds.includes(sessionId)) throw new Error('DSH did not record the workspace membership.')
    process.stdout.write(JSON.stringify({ workspaceId: group.id, sessionId }) + '\n', () => process.exit(0))
  } catch (error) {
    process.stderr.write(String(error) + '\n', () => process.exit(1))
  }
}
