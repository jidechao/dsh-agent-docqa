/**
 * docqa 流式转发插件（dsh profile 覆写层）。
 *
 * 背景：runtime 进程内的 `agent/assistant-stream` 携带 token 级增量
 * （chunk 类型 `text-delta` / `reasoning-delta`），DSH 自己的 Web UI 就是靠它
 * 做流式渲染的；但 Python SDK 的 JSON-RPC 服务端只转发 `session.event` /
 * `session.status` / `subagent.*`，所以走 SDK 的宿主（本项目）拿不到 token。
 *
 * 这个插件补上那一段：订阅 `agent/assistant-stream`，把增量按 ~40ms 一批
 * POST 给 docqa 宿主，由宿主转成 SSE 的 `delta` 事件推给浏览器。
 *
 * 安全与健壮性约定：
 * - 只有设置了 `DOCQA_STREAM_HOOK` 才启用；没设置就完全不动。
 * - 任何异常、任何网络失败都必须被吞掉：转发是旁路，绝不能影响 agent 主流程，
 *   更不能让 runtime 启动失败。
 * - 请求带 `DOCQA_STREAM_TOKEN` 头做校验，避免本机其它进程往宿主乱灌数据。
 */

export const name = 'docqa-stream-forward'

/** 不需要任何服务；纯事件订阅。 */
export const inject = []

/** 攒批窗口：token 级增量直接一条一发会把宿主打成筛子。 */
const FLUSH_MS = 40

export function apply(ctx) {
  const hook = process.env.DOCQA_STREAM_HOOK
  if (!hook) return
  const token = process.env.DOCQA_STREAM_TOKEN || ''

  /** @type {{ sessionId: string, kind: string, index: number, text: string }[]} */
  let pending = []
  let timer = null

  function flush() {
    timer = null
    if (pending.length === 0) return
    const batch = pending
    pending = []
    try {
      const request = fetch(hook, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(token ? { 'x-docqa-token': token } : {}),
        },
        body: JSON.stringify({ deltas: batch }),
      })
      // 失败一律忽略；上游可能还没起来或已经退出。
      if (request && typeof request.catch === 'function') request.catch(() => {})
    } catch {
      /* 永不抛出 */
    }
  }

  function schedule() {
    if (timer !== null) return
    try {
      timer = setTimeout(flush, FLUSH_MS)
      // 不要因为这个定时器拖住进程退出。
      if (timer && typeof timer.unref === 'function') timer.unref()
    } catch {
      timer = null
    }
  }

  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    try {
      if (!frame || frame.type !== 'chunk') return
      const chunk = frame.chunk
      if (!chunk || !chunk.text) return
      const kind = chunk.type === 'text-delta'
        ? 'text'
        : chunk.type === 'reasoning-delta' ? 'reasoning' : null
      if (kind === null) return
      pending.push({
        sessionId: String(agent.session.id),
        kind,
        index: chunk.index,
        text: String(chunk.text),
      })
      schedule()
    } catch {
      /* 永不抛出 */
    }
  })

  ctx.on('dispose', () => {
    try {
      if (timer !== null) clearTimeout(timer)
    } catch {
      /* ignore */
    }
    timer = null
    pending = []
  })
}
