<template>
  <section class="page gap-page" data-module="capture_gap">
    <header class="page-head">
      <div>
        <h2>采集差额</h2>
        <p class="page-desc">
          按实际采集记录逐行关联采集、交接、校验隔离与迟到回执，输出可补采的来源明细。
          流程执行的「产出记录数」只是总数，这里对比交接实际收到的行，定位差额落在哪几个来源。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="seed-demo" type="button" :disabled="busy" @click="seedDemo">
          生成 10 行演示执行
        </button>
        <button class="btn" data-testid="reload" type="button" @click="refresh">刷新</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="notice">{{ notice }}</p>

    <div class="gap-layout">
      <aside class="gap-executions">
        <h3>执行实例</h3>
        <button
          v-for="item in executions"
          :key="item.执行编号"
          class="gap-execution"
          :class="{ active: item.执行编号 === selectedCode }"
          type="button"
          :data-testid="`execution-${item.执行编号}`"
          @click="selectExecution(item.执行编号)"
        >
          <strong>{{ item.执行编号 }}</strong>
          <span>产出 {{ item.执行产出记录数 ?? '—' }} 条 · 交接 {{ item.摘要.交接总数 }} 条</span>
          <span class="gap-chip warn">差额 {{ item.摘要.差额总数 }}</span>
        </button>
        <p v-if="!executions.length" class="empty-state">暂无采集证据，先生成演示执行。</p>
      </aside>

      <div v-if="view" class="gap-main">
        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">产出总数</span>
            <strong class="stat-value" data-testid="stat-produced">{{ view.执行产出记录数 ?? '—' }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">实际采集</span>
            <strong class="stat-value" data-testid="stat-collected">{{ view.摘要.采集总数 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已交接</span>
            <strong class="stat-value" data-testid="stat-received">{{ view.摘要.交接总数 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">差额合计</span>
            <strong class="stat-value" data-testid="stat-gap">{{ view.摘要.差额总数 }}</strong>
          </article>
        </div>

        <section class="gap-scope" data-testid="gap-scope">
          <div class="gap-scope-item confirmed">
            <span class="scope-label">已确认差额</span>
            <strong>{{ view.摘要.已确认差额 }}</strong>
            <span class="scope-sources">{{ joinSources(view.摘要.已确认差额来源) }}</span>
          </div>
          <div class="gap-scope-item pending">
            <span class="scope-label">待核</span>
            <strong>{{ view.摘要.待核 }}</strong>
            <span class="scope-sources">{{ joinSources(view.摘要.待核来源) }}</span>
          </div>
          <div class="gap-scope-item isolated">
            <span class="scope-label">隔离范围</span>
            <strong>{{ view.摘要.隔离 }}</strong>
            <span class="scope-sources">{{ joinSources(view.摘要.隔离来源) }}</span>
          </div>
          <div class="gap-scope-item replenished">
            <span class="scope-label">已补采</span>
            <strong>{{ view.摘要.已补采 }}</strong>
            <span class="scope-sources">{{ joinSources(view.摘要.已补采来源) }}</span>
          </div>
        </section>

        <table class="data-table" data-testid="source-table">
          <thead>
            <tr>
              <th>行号</th>
              <th>来源编号</th>
              <th>采集阶段</th>
              <th>交接阶段</th>
              <th>差额状态</th>
              <th>领用 / 补采</th>
              <th>核对操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in view.来源明细"
              :key="row.来源编号"
              :data-testid="`source-${row.来源编号}`"
              :data-gap-state="row.差额状态"
            >
              <td>{{ row.行号 }}</td>
              <td>{{ row.来源编号 }}</td>
              <td>
                <div>{{ row.采集阶段.采集时间 || '—' }}</div>
                <small>{{ row.采集阶段.来源类型 }} · {{ row.采集阶段.证据摘要 || '无摘要' }}</small>
              </td>
              <td>
                <div>{{ row.交接阶段.接收状态 || '未接收' }}</div>
                <small v-if="row.交接阶段.迟到">迟到回执 · {{ row.交接阶段.接收时间 }}</small>
                <small v-else-if="row.交接阶段.交接编号">{{ row.交接阶段.交接编号 }}</small>
              </td>
              <td>
                <span class="gap-chip" :class="stateClass(row.差额状态)">{{ row.差额状态 }}</span>
              </td>
              <td>
                <div v-if="row.领用编号">{{ row.领用编号 }} · {{ row.领用状态 }}</div>
                <div v-if="row.补采请求">{{ row.补采请求 }} · {{ row.补采请求状态 }}</div>
                <span v-if="!row.领用编号 && !row.补采请求">—</span>
              </td>
              <td>
                <button
                  v-if="row.差额状态 === '待核' && canOperate"
                  class="link"
                  type="button"
                  :data-testid="`confirm-${row.来源编号}`"
                  @click="confirmSource(row.来源编号)"
                >
                  确认缺失
                </button>
                <button
                  v-else-if="row.是否可补采 && canOperate"
                  class="link"
                  type="button"
                  :data-testid="`claim-${row.来源编号}`"
                  @click="claimSource(row.来源编号)"
                >
                  认领补采
                </button>
                <span v-else>—</span>
              </td>
            </tr>
            <tr v-if="!view.来源明细.length">
              <td colspan="7" class="empty-state">该执行还没有采集记录</td>
            </tr>
          </tbody>
        </table>

        <section class="gap-panels">
          <div class="gap-panel" data-testid="gap-versions">
            <h3>差额版本与复算</h3>
            <p class="panel-hint">迟到回执到达后用原采集证据复算旧差额，新证据另生成新版本。</p>
            <ul class="gap-list">
              <li v-for="version in view.差额版本" :key="version.差额编号">
                <div>
                  <strong>{{ version.差额编号 }}</strong>
                  <span class="gap-chip">{{ version.状态 }}</span>
                  <small>采集 {{ version.采集总数 }} / 交接 {{ version.交接总数 }} / 差额 {{ version.差额总数 }}</small>
                </div>
                <div class="row-actions">
                  <button class="link" type="button" :data-testid="`recalc-${version.差额编号}`" @click="recalc(version.差额编号)">
                    复算
                  </button>
                  <button class="link" type="button" @click="selectedVersionCode = version.差额编号">查看</button>
                  <button class="link" type="button" @click="makeVersion">生成新差额</button>
                </div>
              </li>
              <li v-if="!view.差额版本.length" class="empty-state">还没有差额版本</li>
            </ul>
            <p v-if="selectedVersion" class="panel-hint" data-testid="version-scope">
              {{ selectedVersion.差额编号 }}：已确认 {{ joinSources(selectedVersion.已确认差额来源) || '无' }}；
              待核 {{ joinSources(selectedVersion.待核来源) || '无' }}；
              隔离 {{ joinSources(selectedVersion.隔离来源) || '无' }}
            </p>
            <div v-if="recalcResult" class="gap-recalc" data-testid="recalc-result">
              <p>
                复算：交接 {{ recalcResult.复算.交接总数 }} / 差额 {{ recalcResult.复算.差额总数 }}
                <span :class="recalcResult.与原记录一致 ? 'ok-text' : 'error-text'">
                  {{ recalcResult.与原记录一致 ? '与原记录一致' : '与原记录不一致' }}
                </span>
              </p>
              <p>
                当前：交接 {{ recalcResult.当前.交接总数 }} / 差额 {{ recalcResult.当前.差额总数 }}
                （差异 交接 {{ signed(recalcResult.当前差异.交接总数) }}，差额 {{ signed(recalcResult.当前差异.差额总数) }}）
              </p>
            </div>
          </div>

          <div class="gap-panel" data-testid="claim-window">
            <h3>领用窗口</h3>
            <p class="panel-hint">认领后在窗口内取数；迟报到达会自动收口旧领用。</p>
            <ul class="gap-list">
              <li v-for="claim in claims" :key="claim.领用编号">
                <div>
                  <strong>{{ claim.来源编号 }}</strong>
                  <span class="gap-chip" :class="{ warn: claim.领用状态 !== '已补齐' && claim.领用状态 !== '已兑现' }">
                    {{ claim.领用状态 }}
                  </span>
                  <small v-if="claim.补齐方式">补齐方式：{{ claim.补齐方式 }}</small>
                  <small v-else>剩余 {{ claim.剩余秒数 }} 秒</small>
                </div>
                <small>{{ claim.领用编号 }}</small>
              </li>
              <li v-if="!claims.length" class="empty-state">领用窗口为空</li>
            </ul>
            <button class="btn" type="button" data-testid="claim-all" @click="claimAll">认领全部可补采来源</button>
            <button class="btn ghost" type="button" data-testid="request-all" @click="requestAll">提交补采请求</button>
          </div>

          <div class="gap-panel" data-testid="late-receipt">
            <h3>回执登记</h3>
            <p class="panel-hint">模拟交接后到达的有效回执；迟报会从待补采范围中剔除。</p>
            <label class="filter-item">
              <span>来源编号（逗号分隔）</span>
              <input v-model="receiptSources" data-testid="receipt-sources" placeholder="SRC-08" />
            </label>
            <label class="filter-item checkbox">
              <input v-model="receiptLate" type="checkbox" data-testid="receipt-late" />
              <span>迟到回执</span>
            </label>
            <button class="btn" type="button" data-testid="submit-receipt" @click="submitReceipt">登记回执</button>
            <p v-if="receiptResult" class="panel-hint">
              已补齐领用 {{ joinSources(receiptResult.已补齐领用) || '无' }}，
              已撤销请求 {{ joinSources(receiptResult.已撤销补采请求) || '无' }}
            </p>
          </div>

          <div class="gap-panel" data-testid="schedule-check">
            <h3>排程漏采核对</h3>
            <p class="panel-hint">把核对结果写入排程，按执行编号与缺失来源范围发起实际补采。</p>
            <div class="row-actions">
              <input v-model="planCode" class="gap-input" data-testid="plan-code" placeholder="关联计划 SCHE-0003" />
              <button class="btn" type="button" data-testid="create-check" @click="createCheck">写入核对记录</button>
            </div>
            <div v-if="check" class="gap-check" data-testid="check-detail">
              <p><strong>{{ check.核对编号 }}</strong> · {{ check.核对状态 }}</p>
              <p>执行编号：{{ check.执行编号 }}</p>
              <p>缺失来源范围：{{ joinSources(check.缺失来源范围) }}</p>
              <p>结果：{{ check.结果摘要 }}</p>
              <ul class="gap-list">
                <li v-for="item in check.补采结果" :key="item.请求编号">
                  <span>{{ item.请求编号 }} · {{ item.来源编号 }}</span>
                  <span class="gap-chip">{{ item.结果 }}</span>
                </li>
              </ul>
              <button class="btn primary" type="button" data-testid="dispatch-check" @click="dispatchCheck">
                发起实际补采
              </button>
            </div>
            <div v-if="dispatchResult" class="gap-dispatch" data-testid="dispatch-result">
              <p>实际补采来源：{{ joinSources(dispatchResult.实际补采来源) || '无' }}</p>
              <p v-for="item in dispatchResult.跳过来源" :key="item.来源编号">
                跳过 {{ item.来源编号 }}：{{ item.原因 }}
              </p>
            </div>
          </div>

          <div class="gap-panel" data-testid="replenish-queue">
            <h3>补采请求</h3>
            <p class="panel-hint">同一来源重复提交只保留一笔，兑现也只能成功一次。</p>
            <ul class="gap-list">
              <li v-for="request in requests" :key="request.请求编号">
                <div>
                  <strong>{{ request.来源编号 }}</strong>
                  <span class="gap-chip" :class="{ warn: request.请求状态 === '待执行' }">{{ request.请求状态 }}</span>
                  <small>{{ request.请求编号 }} · {{ request.关联核对 || '直接补采' }}</small>
                </div>
                <button
                  v-if="request.请求状态 === '待执行'"
                  class="link"
                  type="button"
                  :data-testid="`complete-${request.请求编号}`"
                  @click="completeRequest(request)"
                >
                  兑现补采
                </button>
              </li>
              <li v-if="!requests.length" class="empty-state">暂无补采请求</li>
            </ul>
          </div>
        </section>
      </div>
      <div v-else class="gap-empty">
        <p>左侧选择一条有采集证据的执行，或先生成 10 行演示执行。</p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  claimGapSources,
  completeGapRequest,
  confirmGapSources,
  createGapCheck,
  createGapVersion,
  dispatchGapCheck,
  loadClaimWindow,
  loadGapCheck,
  loadGapOverview,
  loadGapSnapshot,
  loadGapView,
  recalcGapVersion,
  registerGapReceipts,
  requestGapReplenish,
  seedGapDemo,
} from '@/api/backend-service'
import type {
  ClaimRow,
  GapView,
  GapVersion,
  RecalcResult,
  ReplenishRequest,
  ScheduleCheck,
} from '@/api/backend-service'

type OverviewItem = {
  执行编号: string
  执行产出记录数: string | null
  摘要: GapView['摘要']
  差额版本: GapVersion[]
}

const executions = ref<OverviewItem[]>([])
const selectedCode = ref('')
const view = ref<GapView | null>(null)
const claims = ref<ClaimRow[]>([])
const requests = ref<ReplenishRequest[]>([])
const check = ref<ScheduleCheck | null>(null)
const recalcResult = ref<RecalcResult | null>(null)
const dispatchResult = ref<{
  实际补采来源: string[]
  跳过来源: { 来源编号: string; 原因: string }[]
} | null>(null)
const receiptSources = ref('')
const receiptLate = ref(true)
const receiptResult = ref<{ 已补齐领用: string[]; 已撤销补采请求: string[] } | null>(null)
const planCode = ref('SCHE-0003')
const busy = ref(false)
const errorMessage = ref('')
const notice = ref('')

const canOperate = computed(() => Boolean(view.value))
const latestGap = computed(() => view.value?.差额版本.at(-1) ?? null)
const selectedVersionCode = ref('')
const selectedVersion = computed(() =>
  view.value?.差额版本.find((version) => version.差额编号 === selectedVersionCode.value)
  ?? latestGap.value,
)

function joinSources(sources: string[] | undefined): string {
  return (sources ?? []).join('、')
}

function signed(value: number | undefined): string {
  if (!value) return '0'
  return value > 0 ? `+${value}` : String(value)
}

function stateClass(state: string): string {
  if (state === '校验隔离') return 'danger'
  if (state === '已交接（迟报）') return 'late'
  if (state === '确认缺失' || state === '待核') return 'warn'
  if (state === '已补采') return 'ok'
  return ''
}

async function withGuard(task: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    await task()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

async function refresh() {
  await withGuard(loadOverviewAndSelection)
}

async function loadOverviewAndSelection() {
  const overview = await loadGapOverview()
  executions.value = overview.items
  if (!selectedCode.value && overview.items.length) {
    selectedCode.value = overview.items[0].执行编号
  }
  if (selectedCode.value) {
    await loadSelected(selectedCode.value)
  }
}

async function loadSelected(code: string) {
  const [detail, window] = await Promise.all([
    loadGapView(code),
    loadClaimWindow(code),
  ])
  view.value = detail
  claims.value = window.领用窗口

  const latest = detail.差额版本.at(-1)
  if (latest) {
    const snapshot = await loadGapSnapshot(latest.差额编号)
    requests.value = snapshot.补采请求
  } else {
    requests.value = []
  }
  // 新版本差额可能没有再挂核对，沿用最近一版带核对的记录继续处理旧领用。
  const checkCode = [...detail.差额版本]
    .reverse()
    .flatMap((version) => version.关联核对)
    .at(0)
  check.value = checkCode ? await loadGapCheck(checkCode) : null
}

async function selectExecution(code: string) {
  selectedCode.value = code
  recalcResult.value = null
  dispatchResult.value = null
  receiptResult.value = null
  selectedVersionCode.value = ''
  await withGuard(async () => {
    await loadSelected(code)
  })
}

async function seedDemo() {
  await withGuard(async () => {
    const result = await seedGapDemo()
    notice.value = result.message
    selectedCode.value = String(result.entry?.['执行编号'] ?? '')
    await loadOverviewAndSelection()
  })
}

async function confirmSource(source: string) {
  const gap = latestGap.value
  if (!gap) return
  await withGuard(async () => {
    await confirmGapSources(gap.差额编号, [source])
    await loadSelected(selectedCode.value)
  })
}

async function claimSource(source: string) {
  const gap = latestGap.value
  if (!gap) return
  await withGuard(async () => {
    await claimGapSources(gap.差额编号, [source])
    await loadSelected(selectedCode.value)
  })
}

async function claimAll() {
  const gap = latestGap.value
  if (!gap || !gap.可补采来源.length) {
    errorMessage.value = '当前没有可领用来源'
    return
  }
  await withGuard(async () => {
    await claimGapSources(gap.差额编号, gap.可补采来源)
    await loadSelected(selectedCode.value)
  })
}

async function requestAll() {
  const gap = latestGap.value
  if (!gap || !gap.缺失来源范围.length) {
    errorMessage.value = '当前没有缺失来源'
    return
  }
  await withGuard(async () => {
    const result = await requestGapReplenish(gap.差额编号, gap.缺失来源范围)
    notice.value = `新建请求 ${result.新建请求.length} 笔，重复提交 ${result.重复请求.length} 笔`
    await loadSelected(selectedCode.value)
  })
}

async function completeRequest(request: ReplenishRequest) {
  await withGuard(async () => {
    const result = await completeGapRequest(request.请求编号, [request.来源编号], `补采${request.来源编号}`)
    notice.value = result.已经兑现 ? '该请求已兑现，本次未重复写入' : '补采已兑现'
    await loadSelected(selectedCode.value)
  })
}

async function submitReceipt() {
  const sources = receiptSources.value.split(/[,，\s]+/).filter(Boolean)
  if (!sources.length) {
    errorMessage.value = '请填写来源编号'
    return
  }
  await withGuard(async () => {
    const result = await registerGapReceipts(selectedCode.value, sources, receiptLate.value)
    receiptResult.value = result
    notice.value = '回执已登记'
    await loadSelected(selectedCode.value)
  })
}

async function createCheck() {
  const gap = latestGap.value
  if (!gap) return
  await withGuard(async () => {
    const result = await createGapCheck(gap.差额编号, planCode.value)
    check.value = result.核对
    notice.value = `已写入排程漏采核对 ${result.核对编号}`
    await loadSelected(selectedCode.value)
  })
}

async function dispatchCheck() {
  if (!check.value) return
  await withGuard(async () => {
    const result = await dispatchGapCheck(check.value!.核对编号)
    dispatchResult.value = result
    notice.value = `实际补采 ${result.实际补采来源.length} 个来源，跳过 ${result.跳过来源.length} 个`
    await loadSelected(selectedCode.value)
  })
}

async function recalc(gapCode: string) {
  await withGuard(async () => {
    selectedVersionCode.value = gapCode
    recalcResult.value = await recalcGapVersion(gapCode)
  })
}

async function makeVersion() {
  await withGuard(async () => {
    await createGapVersion(selectedCode.value, '前端生成新差额')
    await loadSelected(selectedCode.value)
  })
}

onMounted(refresh)
</script>

<style scoped>
.gap-layout { display: flex; gap: 14px; align-items: flex-start; }
.gap-executions { width: 220px; flex-shrink: 0; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.gap-executions h3 { margin: 0 0 8px; font-size: 14px; }
.gap-execution { display: flex; flex-direction: column; gap: 2px; width: 100%; text-align: left; background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px; margin-bottom: 6px; cursor: pointer; font-size: 12px; }
.gap-execution.active { border-color: var(--brand); background: #eff6ff; }
.gap-main { flex: 1; min-width: 0; }
.gap-scope { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px; }
.gap-scope-item { background: #fff; border: 1px solid var(--border); border-left-width: 4px; border-radius: 8px; padding: 8px 10px; }
.gap-scope-item.confirmed { border-left-color: #ef4444; }
.gap-scope-item.pending { border-left-color: #f59e0b; }
.gap-scope-item.isolated { border-left-color: #7c3aed; }
.gap-scope-item.replenished { border-left-color: #22c55e; }
.scope-label { display: block; font-size: 12px; color: var(--muted); }
.scope-sources { display: block; font-size: 12px; color: var(--muted); margin-top: 2px; word-break: break-all; }
.gap-chip { display: inline-block; padding: 1px 6px; border-radius: 999px; background: #eef2f7; font-size: 11px; }
.gap-chip.warn { background: #fef3c7; color: #92400e; }
.gap-chip.danger { background: #fee2e2; color: #991b1b; }
.gap-chip.late { background: #e0e7ff; color: #3730a3; }
.gap-chip.ok { background: #dcfce7; color: #166534; }
.gap-panels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.gap-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.gap-panel h3 { margin: 0 0 4px; font-size: 14px; }
.panel-hint { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.gap-list { list-style: none; margin: 0 0 10px; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.gap-list li { display: flex; justify-content: space-between; align-items: center; gap: 8px; border-bottom: 1px dashed #eef2f7; padding-bottom: 4px; font-size: 12px; }
.gap-list small { display: block; color: var(--muted); }
.gap-input { border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; font-size: 12px; }
.gap-recalc, .gap-dispatch { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-size: 12px; }
.gap-recalc p, .gap-dispatch p { margin: 0 0 4px; }
.ok-text { color: #166534; }
.notice-text { color: #1d4ed8; font-size: 12px; }
.checkbox { flex-direction: row; align-items: center; gap: 6px; }
.gap-empty { flex: 1; text-align: center; color: var(--muted); padding: 40px; }
</style>
