import type { EngineInterface, Register } from 'claude-code'

// Loaded into both supervised sessions with --plugin-dir. Whenever the prompt box
// goes from empty to holding a draft or back, it writes {"draft": true|false, "head"}
// to $ADVERSARY_DRAFT_FILE, `head` being the draft's first characters. The bridge
// reads that before typing into the pane, and waits while a person has a message
// half written; `head` tells it whether the box still holds its own message.

const HEAD = 60
let reported: string | undefined
let writes: Promise<void> = Promise.resolve()

// Writes go one after another, so an edit's report can't land after the submit's.
function report($: EngineInterface, text: string): Promise<void> {
  writes = writes
    .then(async () => {
      const head = text.slice(0, HEAD)
      if (head === reported) return
      const path = await $.env.get('ADVERSARY_DRAFT_FILE')
      if (!path) return
      await $.fs.write(path, JSON.stringify({ draft: text !== '', head, at: await $.clock.now() }))
      reported = head
    })
    .catch(() => {})
  return writes
}

async function recheck($: EngineInterface): Promise<void> {
  const box = await $.prompt.read()
  await report($, box.text)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await recheck($)
    // Catches a box emptied without an edit event (cleared with a key binding, say).
    $.clock.every(1000, () => void recheck($))
    return started
  })

  on('prompt.edit', async ($, e, next) => {
    const box = await next(e)
    void report($, box.text)
    return box
  })

  on('prompt.submit', async ($, e, next) => {
    void report($, '')
    return next(e)
  })
}
