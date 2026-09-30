/**
 * 界面截图生成脚本。
 *
 * 为什么要有这个脚本，而不是手动截图：
 *   1. README 里的截图必须在 UI 改版后同步更新，手动截很容易漏掉某一页，
 *      结果就是文档里混着新旧两套界面（本项目已经发生过一次）；
 *   2. 视口尺寸必须固定。截图工具所在的面板宽度会变，窄了会把顶栏导航压成
 *      图标模式、触发布局断点，截出来的界面和真实使用完全不是一回事；
 *   3. 多页截图需要先登录。用脚本直接注入令牌，比每页手动登录可靠。
 *
 * 依赖：
 *   - 前端 dev server 已启动（默认 http://127.0.0.1:5173）
 *   - 后端已启动（默认 http://127.0.0.1:8000，用于取登录令牌）
 *   - 本机装有 Chrome 或 Edge（用 puppeteer-core 驱动，不额外下载内核）
 *
 * 用法（在 frontend 目录下）：
 *   node scripts/screenshot.mjs
 *   node scripts/screenshot.mjs --only dashboard,video      # 只截指定页
 *   CAB_SHOT_BASE=http://127.0.0.1:5174 node scripts/screenshot.mjs
 *
 * 可选：让「实时检测」页显示真实检测画面
 *   无摄像头环境下默认只截到未启动的空画面。若要截出正在检测的效果，
 *   先用 ffmpeg 把一段视频转成 Y4M，再用它充当虚拟摄像头：
 *     ffmpeg -i sample.mp4 -t 6 -vf scale=640:360,fps=12 -pix_fmt yuv420p fake.y4m
 *     CAB_SHOT_FAKE_CAM=fake.y4m node scripts/screenshot.mjs --only realtime
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..', '..')
const OUT_DIR = path.join(ROOT, 'docs', 'images')

const APP = (process.env.CAB_SHOT_BASE || 'http://127.0.0.1:5173').replace(/\/$/, '')
const API = (process.env.CAB_SHOT_API || 'http://127.0.0.1:8000').replace(/\/$/, '')
const USER = process.env.CAB_SHOT_USER || 'admin'
const PASS = process.env.CAB_SHOT_PASS || 'admin123'
const FAKE_CAM = process.env.CAB_SHOT_FAKE_CAM || ''

// 固定视口：与项目实际演示分辨率一致。宽度必须大于顶栏导航的图标档阈值，
// 否则截出来的是"只显示图标"的紧凑布局，无法体现设计。
const VIEWPORT = { width: 1600, height: 950, deviceScaleFactor: 1 }

const BROWSERS = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium'
]

/**
 * 每一页：输出文件名、路由、等待时长（毫秒）、进入后执行的准备动作、截图前的自检。
 *
 * prep / check 一律写成**真实的函数**，不要写成字符串。
 * 写成字符串时 page.evaluate 只会求值出这个函数对象而不会调用它，
 * 结果是"准备工作静默失效"——截图看着正常，内容其实是空的
 * （本项目就踩过：摄像头没开、记录没选中，两张图都成了无意义的空壳）。
 */
const PAGES = [
  { file: 'sentinel-login.png', route: '/login', wait: 1200, auth: false,
    note: '登录页' },
  { file: 'sentinel-dashboard.png', route: '/dashboard', wait: 3800,
    note: '态势总览（含趋势图，需等图表动画结束）',
    check: () => '图表 canvas ' + document.querySelectorAll('canvas').length + ' 块' },
  { file: 'sentinel-realtime.png', route: '/realtime', wait: 6500,
    note: '实时检测',
    // 有虚拟摄像头时主动开启，让截图呈现"正在检测"的真实形态
    prep: async () => {
      const btn = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('开启摄像头'))
      if (btn) {
        btn.click()
        await new Promise((r) => setTimeout(r, 5200))
      }
    },
    // 关键自检：摄像头没起来 / WebSocket 没连上，就只是空画面，不能当"检测中"的截图用
    check: () => {
      const v = document.querySelector('video')
      const chip = document.querySelector('.status-chip')
      return '视频 ' + (v ? v.videoWidth + 'x' + v.videoHeight : '无') +
        '，状态「' + (chip ? chip.textContent.trim() : '?') + '」'
    } },
  { file: 'sentinel-video.png', route: '/video', wait: 3200,
    note: '视频检测（选中一条有检出结果的记录以展示时间轴）',
    // 必须挑一条"事件数不为 0"的记录：排在前面的大概率是空记录，
    // 选到它右侧就只剩空状态，截图失去意义
    prep: async () => {
      const recs = [...document.querySelectorAll('.rec')]
      const withEvents = recs.find((r) => /事件\s*[1-9]/.test(r.textContent))
      const target = withEvents || recs[0]
      if (target) {
        target.click()
        await new Promise((r) => setTimeout(r, 2600))
      }
    },
    check: () => '检测记录 ' + document.querySelectorAll('.rec').length +
      ' 条，事件卡片 ' + document.querySelectorAll('.ecard').length + ' 张' },
  { file: 'sentinel-wall.png', route: '/wall', wait: 2600, note: '点位态势墙' },
  { file: 'sentinel-alarms.png', route: '/alarms', wait: 2600, note: '报警处置' },
  { file: 'sentinel-history.png', route: '/history', wait: 2600, note: '历史取证' },
  { file: 'sentinel-keywords.png', route: '/keywords', wait: 2600, note: '关键词管理',
    check: () => document.querySelectorAll('tbody tr').length + ' 行词条' },
  { file: 'sentinel-persons.png', route: '/persons', wait: 2600, note: '人员管理' },
  { file: 'sentinel-settings.png', route: '/settings', wait: 2600, note: '系统设置',
    check: () => document.querySelectorAll('.cap-card, .param-row').length + ' 个设置项' },
  { file: 'sentinel-users.png', route: '/users', wait: 2600, note: '用户与权限' },
  { file: 'sentinel-cameras.png', route: '/cameras', wait: 2600, note: '点位管理' }
]

function findBrowser() {
  for (const p of BROWSERS) if (fs.existsSync(p)) return p
  throw new Error('未找到 Chrome / Edge，可设置环境变量 CAB_SHOT_BROWSER 指定路径')
}

async function login() {
  const res = await fetch(`${API}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: USER, password: PASS })
  })
  if (!res.ok) throw new Error(`登录失败 HTTP ${res.status}，请确认后端已启动`)
  const data = await res.json()
  if (!data.access_token) throw new Error('登录响应中没有 access_token')
  return data.access_token
}

async function main() {
  const only = (() => {
    const i = process.argv.indexOf('--only')
    return i > -1 && process.argv[i + 1] ? new Set(process.argv[i + 1].split(',')) : null
  })()

  fs.mkdirSync(OUT_DIR, { recursive: true })
  const token = await login()
  console.log(`已获取登录令牌（${API}）`)

  const args = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--hide-scrollbars',
    '--force-device-scale-factor=1',
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream'
  ]
  if (FAKE_CAM && fs.existsSync(FAKE_CAM)) args.push(`--use-file-for-fake-video-capture=${FAKE_CAM}`)

  const browser = await puppeteer.launch({
    executablePath: findBrowser(),
    headless: 'new',
    args,
    defaultViewport: VIEWPORT
  })

  const results = []
  try {
    const page = await browser.newPage()
    // 先落到应用源上，才能写 localStorage
    await page.goto(`${APP}/login`, { waitUntil: 'domcontentloaded' })
    await page.evaluate((t) => localStorage.setItem('cab_token', t), token)

    for (const spec of PAGES) {
      const key = spec.file.replace('sentinel-', '').replace('.png', '')
      if (only && !only.has(key)) continue

      // 登录页要清掉令牌，否则路由守卫会直接跳走
      if (spec.auth === false) await page.evaluate(() => localStorage.removeItem('cab_token'))
      else await page.evaluate((t) => localStorage.setItem('cab_token', t), token)

      await page.goto(`${APP}${spec.route}`, { waitUntil: 'networkidle2', timeout: 60000 })
      if (spec.prep) await page.evaluate(spec.prep)
      await new Promise((r) => setTimeout(r, spec.wait))

      const file = path.join(OUT_DIR, spec.file)
      await page.screenshot({ path: file })
      const kb = Math.round(fs.statSync(file).size / 1024)
      // 自检：内容为空说明准备工作没生效或接口失败。
      // 这时截到的是一张看似正常、实则无意义的图，必须立刻暴露出来。
      let check = ''
      if (spec.check) {
        try {
          check = await page.evaluate(spec.check)
        } catch {
          check = '（自检执行失败）'
        }
      }
      results.push({ file: spec.file, kb, note: spec.note, check })
      console.log(`  ✓ ${spec.file.padEnd(30)} ${String(kb).padStart(4)} KB   ${check || spec.note}`)
    }
  } finally {
    await browser.close()
  }

  console.log(`\n共生成 ${results.length} 张（视口 ${VIEWPORT.width}x${VIEWPORT.height}）→ ${OUT_DIR}`)
}

main().catch((e) => {
  console.error('截图失败：', e.message)
  process.exit(1)
})
