import { expect, mock, test } from 'claude-code/testing'

// prompt.edit comes only from the terminal's editor, so these drive the box
// through prompt.read (the once-a-second recheck) and prompt.submit.
test('reports a draft appearing and the box clearing on submit', async ($, on) => {
  mock.env(on, { ADVERSARY_DRAFT_FILE: '/run/adv/draft-worker.json' })
  const clock = mock.clock(on)
  let box = ''
  const written: { path: string; draft: boolean }[] = []
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  on('prompt.read', () => ({ value: { text: box, cursor: box.length } }))
  on('fs.write', (_$, e) => {
    written.push({ path: e.path, draft: JSON.parse(e.text).draft })
    return { value: undefined }
  })

  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  expect(written).toEqual([{ path: '/run/adv/draft-worker.json', draft: false }])

  box = 'half a mess'
  await clock.advance(1000)
  expect(written.at(-1)).toEqual({ path: '/run/adv/draft-worker.json', draft: true })

  await clock.advance(3000)                                  // unchanged: nothing more written
  expect(written.length).toBe(2)

  box = 'half a message'                                     // the start changed: written again
  await clock.advance(1000)
  expect(written.length).toBe(3)

  box = ''
  await $.prompt.submit({ text: 'half a message' })
  await clock.settle()
  expect(written.at(-1)?.draft).toBe(false)
  expect(written.length).toBe(4)
})

test('writes nothing outside a supervised run', async ($, on) => {
  mock.env(on, {})
  const clock = mock.clock(on)
  let writes = 0
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('prompt.read', () => ({ value: { text: 'typing', cursor: 6 } }))
  on('fs.write', () => {
    writes += 1
    return { value: undefined }
  })
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await clock.advance(2000)
  expect(writes).toBe(0)
})
