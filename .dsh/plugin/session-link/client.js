/**
 * Session Link — Web Client half (v2).
 *
 * Ambience under the composer:
 *   - master: one compact row per linked slave with live status, digest age,
 *     digest expansion, and actions (open / digest / stop / pause-resume /
 *     detach) plus a picker that attaches an existing session through the
 *     `/link` user command;
 *   - slave: shows the attached master, report prompt and "open master".
 *
 * State arrives through persisted session events produced by the Host half:
 * `tool/result` presentation meta (`sessionLink`), `/link` `command/done` text
 * markers, and incoming `user/message` link sources.
 */

window.__ModuleLoader__.load({
  id: '@local/dsh-session-link',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const NS = 'session-link'
    const en = {
      links: 'Session Links',
      link: 'link',
      no_links: 'No linked sessions',
      attach: 'Link session',
      attach_hint: 'Attach an existing top-level session as a slave',
      choose: 'choose a session…',
      cancel: 'Cancel',
      open: 'Open this session',
      digest: 'Generate a subagent digest',
      stop: 'Interrupt the current turn and pause the link',
      pause: 'Pause link',
      resume: 'Resume link',
      detach: 'Detach link',
      slave_of: 'slave of',
      report: 'Report progress',
      report_prompt: 'Report to master:',
      open_master: 'Open master session',
      digest_none: 'no digest yet',
      digest_old: 'digest',
      show_digest: 'View stored digest in the conversation',
      last_sent: 'sent',
      pending_claim: 'pending',
      interjection_pending: 'master interjection pending',
      state_active: 'linked',
      state_paused: 'paused',
      live_running: 'running',
      live_idle: 'idle',
      live_cold: 'cold',
      link_count: 'links',
    }
    const zh = {
      links: '会话链接',
      link: '链接',
      no_links: '没有链接会话',
      attach: '链接会话',
      attach_hint: '把一个已有的顶层会话挂为从属',
      choose: '选择一个会话…',
      cancel: '取消',
      open: '打开该会话',
      digest: '用子代理生成摘要',
      stop: '中断当前 turn 并暂停链接',
      pause: '暂停链接',
      resume: '恢复链接',
      detach: '解除链接',
      slave_of: '从属于',
      report: '汇报进度',
      report_prompt: '向主会话汇报：',
      open_master: '打开主会话',
      digest_none: '暂无摘要',
      digest_old: '摘要',
      show_digest: '在对话中查看摘要',
      last_sent: '已发送',
      pending_claim: '待领取',
      interjection_pending: '主会话插话待处理',
      state_active: '已链接',
      state_paused: '已暂停',
      live_running: '运行中',
      live_idle: '空闲',
      live_cold: '未激活',
      link_count: '条链接',
    }

    function applyLinkCommand(state, sessionId, rawArgs, text) {
      const parts = String(rawArgs || '').trim().split(/\s+/)
      const verb = (parts[0] || 'status').toLowerCase()
      const target = parts[1]
      const base = state && state.role === 'master'
        ? state
        : { v: 1, role: 'master', sessionId, links: [] }
      const links = Array.isArray(base.links) ? base.links.map((link) => ({ ...link })) : []
      const index = () => links.findIndex((link) => link.slaveId === target || link.id === target)
      switch (verb) {
        case 'attach': {
          if (target && index() < 0) {
            links.push({
              id: `${sessionId}::${target}`,
              masterId: sessionId,
              slaveId: target,
              state: 'active',
              live: 'cold',
              createdAt: Date.now(),
              updatedAt: Date.now(),
              digest: null,
              digestAt: null,
            })
          }
          break
        }
        case 'detach': {
          const at = index()
          if (at >= 0) links.splice(at, 1)
          break
        }
        case 'pause':
        case 'stop': {
          const at = index()
          if (at >= 0) links[at] = { ...links[at], state: 'paused', updatedAt: Date.now() }
          break
        }
        case 'resume': {
          const at = index()
          if (at >= 0) links[at] = { ...links[at], state: 'active', updatedAt: Date.now() }
          break
        }
        case 'digest': {
          const at = index()
          if (at >= 0) links[at] = { ...links[at], digest: text || null, digestAt: Date.now() }
          break
        }
        case 'send': {
          const at = index()
          if (at >= 0) links[at] = { ...links[at], lastSentAt: Date.now(), updatedAt: Date.now() }
          break
        }
        default:
          break
      }
      return { ...base, links }
    }

    function foldState(entries, sessionId) {
      let state
      const runs = new Map()
      for (const entry of entries) {
        if (!entry || entry.type !== 'event') continue
        const event = entry.event
        if (!event) continue
        if (event.type === 'tool/result') {
          const meta = event.data && event.data.meta
          if (meta && meta.sessionLink) state = meta.sessionLink
          continue
        }
        if (event.type === 'command/run') {
          const data = event.data || {}
          runs.set(data.commandId, { name: data.name, args: data.args })
          continue
        }
        if (event.type === 'command/done') {
          const data = event.data || {}
          const run = runs.get(data.commandId)
          if (run && run.name === 'link' && data.kind === 'success') {
            state = applyLinkCommand(state, sessionId, run.args, data.text)
          }
          continue
        }
        if (event.type === 'user/message') {
          const source = event.data && event.data.source
          if (source && source.kind === 'session-link' && source.direction === 'master-to-slave') {
            state = {
              v: 1,
              role: 'slave',
              sessionId: source.senderSessionId,
              masterId: source.senderSessionId,
              linkId: source.linkId,
              state: 'active',
            }
          }
        }
      }
      return state
    }

    function shortId(id) {
      return id ? `${String(id).slice(0, 8)}…` : '?'
    }

    function relativeTime(at) {
      if (!at) return ''
      const seconds = Math.max(0, Math.round((Date.now() - at) / 1000))
      if (seconds < 60) return `${seconds}s`
      const minutes = Math.round(seconds / 60)
      if (minutes < 60) return `${minutes}m`
      return `${Math.round(minutes / 60)}h`
    }

    const S = {
      bar: {
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: '6px',
        margin: '2px 4px 0',
        padding: '4px 8px',
        fontSize: '11px',
        lineHeight: '18px',
        color: 'var(--dsw-alias-text-secondary, #8b949e)',
        background: 'var(--dsw-alias-surface-sunken, rgba(127,127,127,0.08))',
        border: '1px solid var(--dsw-alias-border-subtle, rgba(127,127,127,0.25))',
        borderRadius: '8px',
      },
      row: { display: 'inline-flex', alignItems: 'center', gap: '4px' },
      cell: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
        padding: '0 4px',
        borderLeft: '1px solid var(--dsw-alias-border-subtle, rgba(127,127,127,0.2))',
      },
      dot: (live) => ({
        width: '7px',
        height: '7px',
        borderRadius: '50%',
        background: live === 'running' ? '#2ea043' : live === 'idle' ? '#d29922' : 'rgba(127,127,127,0.55)',
      }),
      btn: {
        font: 'inherit',
        fontSize: '11px',
        padding: '0 3px',
        color: 'inherit',
        background: 'transparent',
        border: 'none',
        cursor: 'pointer',
        lineHeight: '16px',
      },
      sep: { opacity: 0.5 },
      muted: { opacity: 0.7 },
      select: {
        font: 'inherit',
        fontSize: '11px',
        maxWidth: '160px',
        color: 'inherit',
        background: 'var(--dsw-alias-surface-raised, transparent)',
        border: '1px solid var(--dsw-alias-border-subtle, rgba(127,127,127,0.3))',
        borderRadius: '4px',
      },
      digestLine: {
        flexBasis: '100%',
        display: 'flex',
        alignItems: 'flex-start',
        gap: '6px',
        padding: '0 4px 2px',
        whiteSpace: 'pre-wrap',
        opacity: 0.85,
      },
    }

    function SessionLinkPanel(props) {
      const sessionId = props.sessionId
      const useSessions = props.useSessions
      const [state, setState] = React.useState(null)
      const [rowsJson, setRowsJson] = React.useState('[]')
      const [picker, setPicker] = React.useState(false)
      const [pick, setPick] = React.useState('')
      const [, setTick] = React.useState(0)
      const sessionRef = React.useRef(null)
      const api = SessionLinkPanel.api || {}
      const sessions = api.sessions
      const uiWorkspace = api.uiWorkspace
      const t = (key) => {
        if (typeof api.translate !== 'function') return en[key] || key
        try {
          const translated = api.translate(key)
          return translated === key ? (en[key] || key) : translated
        } catch {
          return en[key] || key
        }
      }

      React.useEffect(() => {
        if (!api.locale || typeof api.locale.subscribe !== 'function') return undefined
        return api.locale.subscribe(() => setTick((value) => value + 1))
      }, [api.locale])

      React.useEffect(() => {
        const timer = setInterval(() => setTick((value) => value + 1), 30000)
        return () => clearInterval(timer)
      }, [])

      React.useEffect(() => {
        if (!sessionId || !sessions) return undefined
        let disposed = false
        let unsubscribe = null
        let reference = null
        try {
          reference = sessions.retain(sessionId, { source: 'controllerOperation' })
        } catch {
          return undefined
        }
        reference.ready
          .then((binding) => {
            if (disposed) return
            sessionRef.current = binding.session
            const source = binding.eventSource
            const update = () => {
              const next = foldState(source.getSnapshot().entries, sessionId)
              if (next) setState(next)
            }
            update()
            unsubscribe = source.subscribe(update)
          })
          .catch(() => {})
        return () => {
          disposed = true
          if (unsubscribe) unsubscribe()
          sessionRef.current = null
          if (reference) reference.release()
        }
      }, [sessionId, sessions])

      const selectRows = (snapshot) => {
        const ids = (snapshot && snapshot.ids) || []
        const byId = (snapshot && snapshot.byId) || {}
        const rows = []
        for (const id of ids) {
          const row = byId[id]
          if (!row) continue
          rows.push({
            id,
            title: row.displayTitle || row.title || id,
            running: Boolean(row.running),
            subagent: row.origin === 'subagent',
            cwd: row.cwd,
          })
          if (rows.length >= 200) break
        }
        return JSON.stringify(rows)
      }
      const selectedRows = typeof useSessions === 'function' ? useSessions(selectRows) : '[]'
      React.useEffect(() => {
        if (typeof selectedRows === 'string') setRowsJson(selectedRows)
      }, [selectedRows])

      const inbox = typeof props.useProjection === 'function' ? props.useProjection('inbox') : undefined
      const pendingInterjections = (inbox && Array.isArray(inbox['next-step']) ? inbox['next-step'] : [])
        .filter((message) => message && message.source && message.source.kind === 'session-link').length

      const rows = React.useMemo(() => {
        try {
          return JSON.parse(rowsJson)
        } catch {
          return []
        }
      }, [rowsJson])
      const byId = React.useMemo(() => {
        const map = {}
        for (const row of rows) map[row.id] = row
        return map
      }, [rows])

      const run = (line) => {
        const session = sessionRef.current
        if (session) session.command(line)
      }
      const open = (id) => {
        if (uiWorkspace && id) uiWorkspace.openSession(id)
      }

      if (!sessionId) return null

      const role = state && state.role
      const links = (state && state.links) || []
      const linkedIds = new Set(links.map((link) => link.slaveId))
      const stateLabel = (value) => (value === 'paused' ? t('state_paused') : t('state_active'))
      const liveLabel = (value) => (value === 'running' ? t('live_running') : value === 'cold' ? t('live_cold') : t('live_idle'))

      if (role === 'slave' && state.masterId) {
        return h('div', { style: S.bar },
          h('span', null, `${t('links')} · ${t('slave_of')} ${shortId(state.masterId)}`),
          h('span', { style: S.muted }, stateLabel(state.state)),
          pendingInterjections > 0
            ? h('span', { style: S.muted }, `· ${t('interjection_pending')} (${pendingInterjections})`)
            : null,
          h('button', { style: S.btn, title: t('open_master'), onClick: () => open(state.masterId) }, `↗ ${t('open_master')}`),
          h('button', { style: S.btn, title: t('report'), onClick: () => {
            const text = window.prompt(t('report_prompt'))
            if (text && text.trim()) run(`/link report ${text.trim()}`)
          } }, t('report')),
        )
      }

      if (role === 'master' && links.length > 0) {
        const body = []
        for (const link of links) {
          const row = byId[link.slaveId]
          const title = row ? row.title : shortId(link.slaveId)
          const live = row ? (row.running ? 'running' : 'idle') : (link.live || 'cold')
          body.push(h('span', { key: `cell-${link.id}`, style: S.cell, title: link.slaveId },
            h('span', { style: S.dot(live) }),
            h('button', { style: S.btn, title: t('open'), onClick: () => open(link.slaveId) }, title),
            h('span', { style: S.muted }, `[${stateLabel(link.state)} · ${liveLabel(live)}]`),
            h('button', { style: S.btn, title: t('digest'), onClick: () => run(`/link digest ${link.slaveId}`) }, 'Σ'),
            h('button', { style: S.btn, title: t('stop'), onClick: () => run(`/link stop ${link.slaveId}`) }, '⏹'),
            link.state === 'paused'
              ? h('button', { style: S.btn, title: t('resume'), onClick: () => run(`/link resume ${link.slaveId}`) }, '▶')
              : h('button', { style: S.btn, title: t('pause'), onClick: () => run(`/link pause ${link.slaveId}`) }, '⏸'),
            h('button', { style: S.btn, title: t('detach'), onClick: () => run(`/link detach ${link.slaveId}`) }, '✕'),
            link.digest
              ? h('button', {
                  style: S.btn,
                  title: t('show_digest'),
                  onClick: () => run(`/link show ${link.slaveId}`),
                }, `${t('digest_old')}${link.digestAt ? ` · ${relativeTime(link.digestAt)}` : ''}`)
              : h('span', { style: S.muted }, t('digest_none')),
            link.lastSentAt
              ? h('span', { style: S.muted }, `· ${t('last_sent')} ${relativeTime(link.lastSentAt)}`)
              : null,
          ))
        }
        return h('div', { style: S.bar },
          h('span', null, `${t('links')} (${links.length})`),
          ...body,
          h('button', { style: S.btn, title: t('attach_hint'), onClick: () => setPicker(!picker) }, picker ? t('cancel') : `+ ${t('attach')}`),
          picker ? h('span', { style: S.row },
            h('select', {
              style: S.select,
              value: pick,
              onChange: (event) => setPick(event.target.value),
            },
              h('option', { value: '' }, t('choose')),
              ...rows
                .filter((row) => row.id !== sessionId && !row.subagent && row.cwd && !linkedIds.has(row.id))
                .map((row) => h('option', { key: row.id, value: row.id }, `${row.title} (${shortId(row.id)})`)),
            ),
            h('button', { style: S.btn, onClick: () => { if (pick) { run(`/link attach ${pick}`); setPicker(false); setPick('') } } }, t('attach')),
          ) : null,
        )
      }

      return h('div', { style: S.bar },
        h('button', { style: S.btn, title: t('attach_hint'), onClick: () => setPicker(!picker) }, picker ? t('cancel') : `🔗 ${t('attach')}`),
        picker ? h('span', { style: S.row },
          h('select', {
            style: S.select,
            value: pick,
            onChange: (event) => setPick(event.target.value),
          },
            h('option', { value: '' }, t('choose')),
            ...rows
              .filter((row) => row.id !== sessionId && !row.subagent && row.cwd)
              .map((row) => h('option', { key: row.id, value: row.id }, `${row.title} (${shortId(row.id)})`)),
          ),
          h('button', { style: S.btn, onClick: () => { if (pick) { run(`/link attach ${pick}`); setPicker(false); setPick('') } } }, t('attach')),
        ) : null,
      )
    }

    return {
      inject: ['slots', 'sessions', 'uiWorkspace', 'locale'],
      apply(ctx) {
        const api = {
          sessions: ctx.get('sessions'),
          uiWorkspace: ctx.get('uiWorkspace'),
          locale: ctx.get('locale'),
          translate: undefined,
        }
        try {
          if (api.locale && typeof api.locale.register === 'function') {
            api.locale.register(NS, 'en', en)
            api.locale.register(NS, 'zh', zh)
            api.translate = api.locale.bind(NS)
          }
        } catch (error) {
          console.error('session-link: locale registration failed', error)
        }
        SessionLinkPanel.api = api
        ctx.slots.inject('conversation.composer.dock', () => ctx.slots.register({
          name: 'conversation.composer.dock',
          id: 'session-link',
          order: 40,
        }, SessionLinkPanel))
      },
    }
  },
})
