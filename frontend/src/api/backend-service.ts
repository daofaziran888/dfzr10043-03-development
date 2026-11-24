import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 采集差额视图 ──────────────────────────────────────────────

export type GapSourceRow = {
  行号: number
  来源编号: string
  校验状态: string
  阶段: string
  差额状态: string
  是否可补采: boolean
  领用编号: string
  领用状态: string
  补采请求: string
  补采请求状态: string
  采集阶段: { 采集时间: string; 采集批次: string; 证据摘要: string; 来源类型: string; 证据版本: number }
  交接阶段: { 接收状态: string; 迟到: boolean; 交接编号: string; 接收时间: string }
}

export type GapSummary = {
  执行编号: string
  采集总数: number
  交接总数: number
  差额总数: number
  已确认差额: number
  待核: number
  隔离: number
  已补采: number
  已确认差额来源: string[]
  待核来源: string[]
  隔离来源: string[]
  已补采来源: string[]
  缺失来源范围: string[]
  可补采来源: string[]
  迟到来源: string[]
}

export type GapVersion = {
  差额编号: string
  执行编号: string
  证据版本: number
  差额版本: number
  状态: string
  采集总数: number
  交接总数: number
  差额总数: number
  已确认差额: number
  待核: number
  隔离: number
  已补采: number
  已确认差额来源: string[]
  待核来源: string[]
  隔离来源: string[]
  缺失来源范围: string[]
  可补采来源: string[]
  迟到来源: string[]
  快照摘要: string
  关联核对: string[]
  补采结果: { 请求编号: string; 来源编号: string; 证据摘要: string }[]
  生成时间: string
}

export type GapView = {
  执行编号: string
  执行产出记录数: string | null
  证据来源数: number
  摘要: GapSummary
  来源明细: GapSourceRow[]
  差额版本: GapVersion[]
}

export type GapOverview = {
  items: {
    执行编号: string
    执行产出记录数: string | null
    摘要: GapSummary
    差额版本: GapVersion[]
  }[]
  total: number
}

export type ClaimRow = {
  领用编号: string
  差额编号: string
  执行编号: string
  来源编号: string
  领用状态: string
  领用人: string
  领用时间: string
  窗口截止: string
  剩余秒数: number
  是否超窗: boolean
  补齐方式: string
}

export type ReplenishRequest = {
  请求编号: string
  差额编号: string
  执行编号: string
  来源编号: string
  请求状态: string
  请求人: string
  关联核对: string
  兑现时间: string
  撤销原因: string
}

export type ScheduleCheck = {
  核对编号: string
  关联计划: string
  执行编号: string
  差额编号: string
  缺失来源范围: string[]
  核对状态: string
  核对人: string
  核对时间: string
  结果摘要: string
  补采请求: string[]
  补采结果: { 请求编号: string; 来源编号: string; 结果: string }[]
}

export type RecalcResult = {
  差额编号: string
  执行编号: string
  快照摘要: string
  复算: GapSummary
  当前: GapSummary
  与原记录一致: boolean
  当前差异: Record<string, number>
}

async function gapPost<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

async function gapGet<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export const loadGapOverview = () => gapGet<GapOverview>('/api/gap')
export const loadGapView = (code: string) => gapGet<GapView>(`/api/gap/${encodeURIComponent(code)}`)
export const loadGapSnapshot = (gapCode: string) =>
  gapGet<GapVersion & { 来源明细: GapSourceRow[]; 领用记录: ClaimRow[]; 补采请求: ReplenishRequest[] }>(
    `/api/gap-snapshot/${encodeURIComponent(gapCode)}`,
  )
export const seedGapDemo = () => gapPost<ActionResult>('/api/gap-demo/seed')
export const createGapVersion = (code: string, reason: string) =>
  gapPost<GapVersion>(`/api/gap/${encodeURIComponent(code)}/snapshots`, { reason })
export const recalcGapVersion = (gapCode: string) =>
  gapPost<RecalcResult>(`/api/gap-snapshot/${encodeURIComponent(gapCode)}/recalc`)
export const confirmGapSources = (gapCode: string, sources: string[]) =>
  gapPost<GapVersion>(`/api/gap-snapshot/${encodeURIComponent(gapCode)}/confirm`, { sources })
export const claimGapSources = (gapCode: string, sources: string[]) =>
  gapPost<{ ok: boolean; 新建领用: ClaimRow[]; 重复领用: string[]; 领用窗口: ClaimRow[] }>(
    `/api/gap-snapshot/${encodeURIComponent(gapCode)}/claims`,
    { sources },
  )
export const loadClaimWindow = (executionCode: string) =>
  gapGet<{ 领用窗口: ClaimRow[]; 待取数: number; 补采中: number; 已补齐: number; 已兑现: number; 已超窗: number }>(
    `/api/gap-claims?execution_code=${encodeURIComponent(executionCode)}&include_closed=true`,
  )
export const requestGapReplenish = (gapCode: string, sources: string[]) =>
  gapPost<{ ok: boolean; 新建请求: ReplenishRequest[]; 重复请求: ReplenishRequest[]; 补采请求: ReplenishRequest[] }>(
    `/api/gap-snapshot/${encodeURIComponent(gapCode)}/requests`,
    { sources },
  )
export const completeGapRequest = (requestCode: string, sources: string[], content: string) =>
  gapPost<{ ok: boolean; 已经兑现: boolean; 补采结果: Record<string, unknown> }>(
    `/api/gap-request/${encodeURIComponent(requestCode)}/complete`,
    { records: sources.map((source) => ({ 来源编号: source, 行号: 0, 证据内容: content, 校验状态: '校验通过' })) },
  )
export const registerGapReceipts = (code: string, sources: string[], late: boolean) =>
  gapPost<{ ok: boolean; 已补齐领用: string[]; 已撤销补采请求: string[] }>(
    `/api/gap/${encodeURIComponent(code)}/receipts`,
    { sources, late },
  )
export const createGapCheck = (gapCode: string, planCode: string) =>
  gapPost<{ ok: boolean; 核对编号: string; 缺失来源范围: string[]; 核对: ScheduleCheck }>(
    `/api/gap-snapshot/${encodeURIComponent(gapCode)}/checks`,
    { plan_code: planCode },
  )
export const loadGapCheck = (checkCode: string) =>
  gapGet<ScheduleCheck>(`/api/gap-check/${encodeURIComponent(checkCode)}`)
export const dispatchGapCheck = (checkCode: string) =>
  gapPost<{
    ok: boolean
    核对缺失来源范围: string[]
    实际补采来源: string[]
    跳过来源: { 来源编号: string; 原因: string }[]
    新建请求: ReplenishRequest[]
    核对: ScheduleCheck
  }>(`/api/gap-check/${encodeURIComponent(checkCode)}/dispatch`)
