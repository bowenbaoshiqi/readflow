/* 书舟 - 阅读器前端(foliate-js)
 * API 参考 foliate-js demo reader.js。foliate-js 无构建步骤,ES module 直接 import。
 */
import { Overlayer } from './foliate-js/overlayer.js'
import './foliate-js/view.js'

const { BOOK_ID, BASE } = window.READFLOW

const view = document.createElement('foliate-view')
document.getElementById('viewer').append(view)

let currentLocation = null  // relocate 事件的 detail

// ---- 打开书籍 + 恢复进度 + 渲染已有划线 ----
async function init() {
  await view.open(`${BASE}/api/books/${BOOK_ID}/file`)

  // 恢复阅读位置(优先 CFI)
  const p = await fetch(`${BASE}/api/books/${BOOK_ID}/progress`).then(r => r.json())
  if (p.cfi) {
    try { await view.goTo(p.cfi) } catch { /* CFI 可能失效,忽略 */ }
  }

  // 加载已有划线
  const hs = await fetch(`${BASE}/api/books/${BOOK_ID}/highlights`).then(r => r.json())
  for (const h of hs) {
    try {
      await view.addAnnotation({ value: h.start_cfi, color: h.color, note: h.text })
    } catch { /* 某些 CFI 可能因书籍变更失效,忽略单条 */ }
  }
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

// ---- 选中文字 → 弹出底部工具栏 ----
view.addEventListener('create-overlay', () => {})
document.addEventListener('selectionchange', () => {
  const sel = window.getSelection()
  const bar = document.getElementById('bottom-bar')
  if (!sel || sel.isCollapsed || sel.toString().trim() === '') {
    bar.hidden = true
    return
  }
  // 选中文字转 CFI(foliate-js view 提供 getCFI(index, range))
  const range = sel.getRangeAt(0)
  const index = currentLocation?.index
  if (index == null) return
  try {
    const cfi = view.getCFI(index, range)
    bar.hidden = false
    bar.dataset.cfi = cfi
    bar.dataset.text = sel.toString()
  } catch { /* 跨章节选区可能失败 */ }
})

// ---- 划线按钮 ----
document.getElementById('bottom-bar').addEventListener('click', async (ev) => {
  if (ev.target?.dataset?.act !== 'highlight') return
  const bar = ev.currentTarget
  const cfi = bar.dataset.cfi
  const text = bar.dataset.text
  if (!cfi) return
  const res = await fetch(`${BASE}/api/books/${BOOK_ID}/highlights`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      spine_index: currentLocation?.index ?? 0,
      start_cfi: cfi, end_cfi: cfi, text
    })
  }).then(r => r.json())
  if (res.ok) {
    try { await view.addAnnotation({ value: cfi, color: 'yellow', note: text }) } catch {}
    bar.hidden = true
    window.getSelection()?.removeAllRanges()
  }
})

document.getElementById('back').onclick = () => history.back()
document.getElementById('toc-btn').onclick = async () => {
  // 简单目录:弹出章节列表
  const toc = view.book?.toc || []
  if (!toc.length) return
  const labels = toc.map(t => t.label).join('\n')
  const idx = prompt(`目录(输入序号):\n${toc.map((t, i) => `${i}: ${t.label}`).join('\n')}`)
  if (idx != null && toc[+idx]) await view.goTo(toc[+idx].href)
}

init().catch(e => {
  document.getElementById('viewer').innerHTML = `<p class="error">打开书籍失败: ${e}</p>`
})
