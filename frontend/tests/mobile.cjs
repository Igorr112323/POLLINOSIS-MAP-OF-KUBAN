/* Optional Chromium geometry and print test; uses a disposable API and database. */
const http = require('node:http')
const net = require('node:net')
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const { spawn } = require('node:child_process')
const root = path.resolve(__dirname, '../..')
const dist = path.join(root, 'frontend/dist')
const python = process.env.BACKEND_PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const sleep = ms => new Promise(r => setTimeout(r, ms))
const assert = (condition, message) => { if (!condition) throw Error(message) }
async function port() {
  return new Promise((resolve, reject) => {
    const s = net.createServer().on('error', reject)
    s.listen(0, '127.0.0.1', () => { const n = s.address().port; s.close(() => resolve(n)) })
  })
}
async function button(page, text) {
  for (const b of await page.$$('button')) if ((await b.evaluate(e => e.textContent)).includes(text)) return b
  throw Error('Button not found: ' + text)
}
async function measure(page, width, label) {
  const result = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth, bodyWidth: document.body.scrollWidth,
    bad: [...document.querySelectorAll('.work-page button,.work-page input:not([type=checkbox]),.work-page select')].filter(e => {
      const r = e.getBoundingClientRect()
      return r.width > 0 && r.height > 0 && (r.right > innerWidth + 1 || r.left < -1 || r.height < 43)
    }).map(e => e.outerHTML.slice(0, 160)),
  }))
  assert(result.documentWidth <= width && result.bodyWidth <= width && !result.bad.length, `${width} ${label}: ${JSON.stringify(result)}`)
  console.log(`PASS ${width}px · ${label}`)
}
async function main() {
  const puppeteer = (await import('puppeteer-core')).default
  assert(process.env.CHROMIUM_PATH, 'Set CHROMIUM_PATH to a compatible Chromium/Chrome executable')
  assert(fs.existsSync(path.join(dist, 'index.html')), 'Run npm run build first')
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pollinoz-mobile-'))
  const apiPort = await port(), webPort = await port()
  let log = '', browser
  const api = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(apiPort)], {
    cwd: path.join(root, 'backend'), env: { ...process.env, DEMO_MODE: 'true', DEMO_ACCOUNTS_ENABLED: 'true', OFFLINE: 'true', DATABASE_URL: 'sqlite:///' + path.join(dir, 'db').replace(/\\/g, '/'), MODEL_PATH: path.join(dir, 'model'), UPLOAD_DIR: path.join(dir, 'uploads') },
  })
  api.stdout.on('data', x => log += x); api.stderr.on('data', x => log += x)
  const server = http.createServer((req, res) => {
    if (req.url.startsWith('/api/')) {
      const out = http.request({ hostname: '127.0.0.1', port: apiPort, path: req.url, method: req.method, headers: req.headers }, r => { res.writeHead(r.statusCode, r.headers); r.pipe(res) })
      out.on('error', () => res.writeHead(502).end()); req.pipe(out); return
    }
    const file = path.join(dist, req.url === '/' ? 'index.html' : req.url.split('?')[0])
    if (!file.startsWith(dist + path.sep)) return res.writeHead(403).end()
    fs.readFile(file, (err, data) => {
      if (err) return res.writeHead(404).end()
      res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html')
      res.end(data)
    })
  })
  try {
    let ready = false
    for (let i = 0; i < 120; i++) { try { if ((await fetch(`http://127.0.0.1:${apiPort}/api/v1/health`)).ok) { ready = true; break } } catch {} await sleep(100) }
    assert(ready, 'Backend startup failed: ' + log)
    await new Promise(resolve => server.listen(webPort, '127.0.0.1', resolve))
    browser = await puppeteer.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] })
    for (const width of [360, 390, 430]) {
      const page = await browser.newPage(), errors = []
      page.on('pageerror', e => errors.push(e.message)); page.on('dialog', dialog => dialog.accept()); await page.setRequestInterception(true); page.on('request', r => r.url().startsWith(`http://127.0.0.1:${webPort}`) ? r.continue() : r.abort())
      await page.setViewport({ width, height: 800, deviceScaleFactor: 1, isMobile: true, hasTouch: true })
      await page.goto(`http://127.0.0.1:${webPort}/`, { waitUntil: 'networkidle0', timeout: 40000 })
      await page.waitForSelector('.leaflet-map')
      await page.click('.mobile-menu'); await (await button(page, 'Войти / роль')).click(); await page.select('select[aria-label="Демо-роли"]', 'dispatcher'); await page.waitForFunction(() => !document.querySelector('.auth-popover'))
      await (await page.$$('.app-tabs button'))[1].click(); await page.waitForSelector('.brigade-list input')
      await page.waitForFunction(() => { const b = [...document.querySelectorAll('button')].find(e => e.textContent.includes('Сформировать пакет нарядов')); return b && !b.disabled })
      if (!await page.$('.table-link')) { await (await button(page, 'Сформировать пакет нарядов')).click(); await page.waitForSelector('.table-link') }
      await (await page.$('.table-link')).click()
      if (await page.evaluate(() => [...document.querySelectorAll('button')].some(e => e.textContent.includes('Взять в работу')))) {
        await page.waitForFunction(() => { const b = [...document.querySelectorAll('button')].find(e => e.textContent.includes('Взять в работу')); return b && !b.disabled })
        await (await button(page, 'Взять в работу')).click()
      }
      await page.waitForSelector('.completion-form'); await measure(page, width, 'field terminal')
      await page.evaluate(() => { window.print = () => { window.__printed = true } })
      await page.waitForFunction(() => { const b=[...document.querySelectorAll('button')].find(e=>e.textContent.includes('Печать ведомости'));return b&&!b.disabled }); const ordersPrint = await button(page, 'Печать ведомости'); await ordersPrint.evaluate(e=>e.scrollIntoView({block:'center'})); await ordersPrint.click(); await page.waitForFunction(() => window.__printed === true); await page.emulateMediaType('print')
      let print = await page.evaluate(() => ({ invoked: window.__printed, sheet: getComputedStyle(document.querySelector('.print-sheet')).display, nav: getComputedStyle(document.querySelector('.app-header')).display, rows: document.querySelectorAll('.print-sheet tbody tr').length, text: document.querySelector('.print-sheet').textContent }))
      assert(print.invoked && print.sheet !== 'none' && print.nav === 'none' && print.rows && print.text.includes('data_provenance'), 'Order print view: '+JSON.stringify({...print,text:print.text?.slice(0,150)}))
      const ordersPdf = await page.pdf({ format: 'A4' }); if (process.env.MOBILE_ARTIFACT_DIR) { fs.mkdirSync(process.env.MOBILE_ARTIFACT_DIR, {recursive:true}); fs.writeFileSync(path.join(process.env.MOBILE_ARTIFACT_DIR, `orders-${width}.pdf`), ordersPdf) } await page.emulateMediaType('screen')
      await page.click('.mobile-menu'); await (await button(page, 'Войти / роль')).click(); await page.select('select[aria-label="Демо-роли"]', 'patient'); await page.waitForFunction(() => !document.querySelector('.auth-popover'))
      await (await page.$$('.app-tabs button'))[2].click(); await page.waitForSelector('input[placeholder="Код или имя"]'); await measure(page, width, 'patient form')
      await page.type('input[placeholder="Код или имя"]', 'Mobile demo'); await page.$eval('.patient-layout form', e => e.requestSubmit()); await page.waitForSelector('.calendar-meta', { timeout: 30000 }); await measure(page, width, 'patient calendar')
      await page.evaluate(() => { window.__printed = false }); const bulletinPrint = await button(page, 'Печать бюллетеня'); await bulletinPrint.evaluate(e=>e.scrollIntoView({block:'center'})); await bulletinPrint.click(); await page.waitForFunction(() => window.__printed === true); await page.emulateMediaType('print')
      print = await page.evaluate(() => ({ invoked: window.__printed, rows: document.querySelectorAll('.print-sheet tbody tr').length, nav: getComputedStyle(document.querySelector('.app-header')).display, text: document.querySelector('.print-sheet').textContent }))
      assert(print.invoked && print.rows === 14 && print.nav === 'none' && print.text.includes('не заменяет консультацию'), 'Bulletin print view')
      const bulletinPdf = await page.pdf({ format: 'A4' }); if (process.env.MOBILE_ARTIFACT_DIR) fs.writeFileSync(path.join(process.env.MOBILE_ARTIFACT_DIR, `bulletin-${width}.pdf`), bulletinPdf); assert(!errors.length, 'Browser errors: ' + errors.join('; ')); console.log(`PASS ${width}px · both print views`)
      await page.close()
    }
  } finally {
    if (browser) await browser.close()
    if (server.listening) await new Promise(resolve => server.close(resolve))
    api.kill('SIGTERM'); await new Promise(resolve => { if (api.exitCode !== null) return resolve(); api.once('exit', resolve); setTimeout(resolve, 5000) })
    fs.rmSync(dir, { recursive: true, force: true })
  }
}
main().catch(e => { console.error(e); process.exitCode = 1 })
