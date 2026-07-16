/* 书舟 - 阅读器前端(foliate-js)
 * API 参考 foliate-js demo reader.js。foliate-js 无构建步骤,ES module 直接 import。
 * 配置从 #viewer 的 data-* 属性读取,避免多 module script 的执行顺序问题。
 */
import { Overlayer } from './foliate-js/overlayer.js'
import './foliate-js/view.js'

const viewer = document.getElementById('viewer')
const BOOK_ID = viewer.dataset.bookId
const BASE = viewer.dataset.base

const view = document.createElement('foliate-view')
viewer.append(view)

let currentLocation = null  // relocate 事件的 detail

// ---- 阅读会话:记录起止 CFI,关书时 POST reading-session ----
let sessionStartCFI = null
let sessionEndCFI = null
let sessionPercentFrom = null
let sessionPercentTo = null

// ---- 打开书籍 + 恢复进度 + 渲染已有划线 ----
async function init() {
  console.log('[readflow] opening book', BOOK_ID, 'from', BASE)
  await view.open(`${BASE}/api/books/${BOOK_ID}/file`)
  console.log('[readflow] book opened, sections:', view.book?.sections?.length)

  // 排版设置:加载用户偏好 + 应用到 foliate 渲染器
  await initTypography()

  // 恢复阅读位置(优先 CFI)
  const p = await fetch(`${BASE}/api/books/${BOOK_ID}/progress`).then(r => r.json())
  if (p.cfi) {
    try { await view.goTo(p.cfi); console.log('[readflow] restored to', p.cfi) }
    catch (e) { console.warn('[readflow] restore failed', e) }
  }

  // 加载已有划线
  const hs = await fetch(`${BASE}/api/books/${BOOK_ID}/highlights`).then(r => r.json())
  for (const h of hs) {
    try { await view.addAnnotation({ value: h.start_cfi, color: h.color, note: h.text }) }
    catch (e) { console.warn('[readflow] highlight load failed', e) }
  }
  console.log('[readflow] init done')
}

// ---- 划线绘制:foliate-js 要求监听 draw-annotation 自己画 ----
view.addEventListener('draw-annotation', e => {
  const { draw, annotation } = e.detail
  draw(Overlayer.highlight, { color: annotation.color || 'yellow' })
})

// ---- 位置变化 → 存进度 ----
view.addEventListener('relocate', e => {
  const { cfi, fraction, index } = e.detail
  currentLocation = e.detail
  // 记录会话起止 CFI
  if (sessionStartCFI === null) {
    sessionStartCFI = cfi
    sessionPercentFrom = fraction || 0
  }
  sessionEndCFI = cfi
  sessionPercentTo = fraction || 0
  document.getElementById('progress').textContent = `${Math.round((fraction || 0) * 100)}%`
  fetch(`${BASE}/api/books/${BOOK_ID}/progress`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      spine_index: index ?? 0,
      cfi: cfi || null,
      percent: fraction || 0
    })
  }).catch(() => {})
})

// ---- 选中文字 → 显示顶部工具栏的划线/复制按钮 ----
// v0.6: 按钮从底部固定浮层移到顶部 #toolbar,避免遮挡正文选区。
// foliate 用 iframe 渲染,选区在 iframe 的 doc 里,必须遍历 renderer.getContents() 取
function getFoliateSelection() {
  const contents = view.renderer?.getContents() ?? []
  for (const { index, doc } of contents) {
    const sel = doc?.defaultView?.getSelection()
    if (sel && !sel.isCollapsed && sel.toString().trim()) {
      const range = sel.getRangeAt(0)
      let cfi = null
      try { cfi = view.getCFI(index, range) } catch {}
      return { index, range, cfi, text: sel.toString() }
    }
  }
  return null
}

// 当前选区状态(划线/复制按钮点击时读)
let selState = null  // { cfi, text, index } | null

function showSelButtons(show) {
  document.getElementById('hl-btn').hidden = !show
  document.getElementById('cp-btn').hidden = !show
}

// 用定时轮询检测选区变化(selectionchange 在 iframe 内不冒泡到主文档)
let selectionPoll = null
let lastSelectionActive = false
function startSelectionPoll() {
  if (selectionPoll) return
  selectionPoll = setInterval(() => {
    const sel = getFoliateSelection()
    if (sel && sel.cfi) {
      selState = { cfi: sel.cfi, text: sel.text, index: sel.index }
      showSelButtons(true)
      lastSelectionActive = true
    } else if (lastSelectionActive) {
      // 选区消失,延迟隐藏(避免划线按钮点击前就被藏)
      setTimeout(() => {
        const still = getFoliateSelection()
        if (!still || !still.cfi) {
          selState = null
          showSelButtons(false)
        }
      }, 150)
      lastSelectionActive = false
    }
  }, 250)
}
startSelectionPoll()

// ---- 划线/复制按钮(事件委托到 #toolbar) ----
document.getElementById('toolbar').addEventListener('click', async (ev) => {
  const act = ev.target?.dataset?.act
  if (!act) return  // 点的是 toolbar 其他按钮(back/toc/typo),不处理

  if (act === 'highlight') {
    if (!selState?.cfi) return
    const { cfi, text } = selState
    const index = Number(selState.index ?? currentLocation?.index ?? 0)
    const res = await fetch(`${BASE}/api/books/${BOOK_ID}/highlights`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        spine_index: index,
        start_cfi: cfi, end_cfi: cfi, text
      })
    }).then(r => r.json())
    if (res.ok) {
      try { await view.addAnnotation({ value: cfi, color: 'yellow', note: text }) } catch {}
      showSelButtons(false)
      try { view.deselect() } catch {}
    }
  } else if (act === 'copy') {
    const text = selState?.text
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      ev.target.textContent = '已复制 ✓'
      setTimeout(() => { showSelButtons(false); ev.target.textContent = '复制' }, 600)
    } catch {
      // 剪贴板 API 在非 HTTPS/localhost 可能失败,降级提示
      alert('复制失败,请手动选择复制:\n' + text.slice(0, 200))
    }
  }
})

document.getElementById('back').onclick = () => location.href = '/'
document.getElementById('toc-btn').onclick = async () => {
  // 简单目录:弹出章节列表
  const toc = view.book?.toc || []
  if (!toc.length) return
  const labels = toc.map(t => t.label).join('\n')
  const idx = prompt(`目录(输入序号):\n${toc.map((t, i) => `${i}: ${t.label}`).join('\n')}`)
  if (idx != null && toc[+idx]) await view.goTo(toc[+idx].href)
}

// ---- 翻页:键盘箭头 + 点击左右半屏 ----
// 点击中间区域不翻页(避免干扰选中文字);点左半屏上一页,右半屏下一页
view.addEventListener('click', e => {
  // 选中文字时不翻页
  const sel = window.getSelection()
  if (sel && !sel.isCollapsed) return
  const rect = view.getBoundingClientRect()
  const x = e.clientX - rect.left
  if (x < rect.width * 0.4) view.goLeft()
  else if (x > rect.width * 0.6) view.goRight()
})

document.addEventListener('keydown', e => {
  // 输入框聚焦时不拦截
  if (e.target?.tagName === 'INPUT' || e.target?.tagName === 'TEXTAREA') return
  if (e.key === 'ArrowLeft' || e.key === 'h') { e.preventDefault(); view.goLeft() }
  else if (e.key === 'ArrowRight' || e.key === 'l' || e.key === ' ') { e.preventDefault(); view.goRight() }
})

init().catch(e => {
  console.error('[readflow] init failed', e)
  const d = document.createElement('pre')
  d.style.cssText = 'padding:24px;color:#c00;white-space:pre-wrap'
  d.textContent = '打开书籍失败: ' + (e?.stack || e)
  document.getElementById('viewer').append(d)
})

// ---- 关书:POST reading-session ----
async function postReadingSession() {
  if (!sessionStartCFI || sessionStartCFI === sessionEndCFI) return
  try {
    await fetch(`${BASE}/api/books/${BOOK_ID}/reading-session`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_cfi: sessionStartCFI,
        end_cfi: sessionEndCFI,
        percent_from: sessionPercentFrom,
        percent_to: sessionPercentTo,
      }),
      keepalive: true,
    })
  } catch {}
}
window.addEventListener('beforeunload', postReadingSession)

// ---- 排版设置:面板 + foliate setStyles 注入 + localStorage 持久化 ----
const TYPO_KEY = 'readflow:typography'
let typoSettings = null   // 当前生效的设置
let typoMeta = null       // {fonts, ranges} 来自后端

// 面板事件绑定:立即执行,不依赖 init/initTypography 成功。
// 若放在异步链末端,任一步抛错 → 绑定没跑 → 按钮无响应(已由 e2e 测试复现)。
bindTypography()

async function initTypography() {
  // 拉后端默认设置 + 字体清单 + 合法范围
  const meta = await fetch(`${BASE}/api/settings/typography`).then(r => r.json())
  typoMeta = meta
  // localStorage 覆盖默认值(用户上次调的)
  const saved = loadSaved()
  typoSettings = { ...meta.defaults, ...saved }
  populateFontChips(meta.fonts)
  syncPanel(typoSettings)
  await applyTypography(typoSettings)
}

function loadSaved() {
  try { return JSON.parse(localStorage.getItem(TYPO_KEY) || '{}') }
  catch { return {} }
}
function saveSaved() {
  try { localStorage.setItem(TYPO_KEY, JSON.stringify(typoSettings)) }
  catch {}
}

// 设置 → 后端生成 CSS → 注入 foliate 渲染器
async function applyTypography(s) {
  const r = await fetch(`${BASE}/api/settings/typography/css`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(s),
  }).then(r => r.json())
  // foliate 用 blob: URL 的 iframe 渲染 epub,@font-face 里的绝对路径
  // url('/static/...') 在 blob 上下文会解析成 blob origin 的根(无效)→
  // 字体零请求、静默失败。补成完整 origin URL,blob iframe 才能正确加载。
  const css = r.css.replace(
    /url\(['"]?\/static\//g,
    `url('${location.origin}/static/`,
  )
  view.renderer?.setStyles?.(css)
}

// 字体清单 → 纸片按钮(用各字体自身渲染,所见即所得)
function populateFontChips(fonts) {
  const box = document.getElementById('typo-font-chips')
  box.innerHTML = ''
  for (const f of fonts) {
    const b = document.createElement('button')
    b.className = 'typo-font-chip'
    b.dataset.font = f.id
    b.textContent = f.name
    b.style.fontFamily = `'${f.family}', serif`
    box.append(b)
  }
}

// 把当前设置反映到面板控件(滑块值、纸片选中态、字号预览)
function syncPanel(s) {
  const size = document.getElementById('typo-size')
  const sp = document.getElementById('typo-spacing')
  size.value = s.fontSize
  document.getElementById('typo-size-val').textContent = s.fontSize
  sp.value = s.spacing
  document.getElementById('typo-spacing-val').textContent = Number(s.spacing).toFixed(1)
  document.getElementById('typo-size-preview').style.fontSize = s.fontSize + 'px'
  for (const b of document.querySelectorAll('#typo-margin-chips button')) {
    b.setAttribute('aria-pressed', b.dataset.margin === s.margin)
  }
  for (const b of document.querySelectorAll('#typo-font-chips button')) {
    b.setAttribute('aria-pressed', b.dataset.font === s.font)
  }
}

function bindTypography() {
  const panel = document.getElementById('typo-panel')
  const btn = document.getElementById('typo-btn')
  const close = document.getElementById('typo-close')

  const open = () => { panel.hidden = false }
  const shut = () => { panel.hidden = true }
  btn.onclick = open
  close.onclick = shut
  document.addEventListener('keydown', e => { if (e.key === 'Escape') shut() })
  // 点面板外收起(不挡正文)
  document.addEventListener('click', e => {
    if (panel.hidden) return
    if (!panel.contains(e.target) && e.target !== btn) shut()
  })

  // 字号 / 行距滑块:拖动实时生效(节流,避免每次拖都发请求)
  // typoSettings 可能在初始化未完成时为 null,此时忽略(不崩)
  let sizeTimer = null
  document.getElementById('typo-size').oninput = e => {
    if (!typoSettings) return
    typoSettings.fontSize = +e.target.value
    document.getElementById('typo-size-val').textContent = typoSettings.fontSize
    document.getElementById('typo-size-preview').style.fontSize = typoSettings.fontSize + 'px'
    clearTimeout(sizeTimer)
    sizeTimer = scheduleApply()
  }
  let spTimer = null
  document.getElementById('typo-spacing').oninput = e => {
    if (!typoSettings) return
    typoSettings.spacing = +e.target.value
    document.getElementById('typo-spacing-val').textContent = typoSettings.spacing.toFixed(1)
    clearTimeout(spTimer)
    spTimer = scheduleApply()
  }

  // 边距 / 字体纸片:点击即换
  document.getElementById('typo-margin-chips').onclick = e => {
    if (!typoSettings) return
    const b = e.target.closest('button[data-margin]'); if (!b) return
    typoSettings.margin = b.dataset.margin
    syncPanel(typoSettings); applyAndSave(typoSettings)
  }
  document.getElementById('typo-font-chips').onclick = e => {
    if (!typoSettings) return
    const b = e.target.closest('button[data-font]'); if (!b) return
    typoSettings.font = b.dataset.font
    syncPanel(typoSettings); applyAndSave(typoSettings)
  }
}

// 节流:拖动滑块时合并连续变更,停手后发一次
function scheduleApply() {
  return setTimeout(() => { applyAndSave(typoSettings) }, 120)
}
function applyAndSave(s) {
  applyTypography(s).catch(e => console.warn('[readflow] apply typo failed', e))
  saveSaved()
}
