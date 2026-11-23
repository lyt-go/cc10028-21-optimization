import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  DeliveryReceipt,
  DeliveryResult,
  EntryRow,
  OverviewResult,
  PageResult,
  VersionDiffResult,
} from '@/data/types'

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

export interface CompareParams {
  docId: string
  versionA: string
  versionB: string
  receiverId: string
  hasBaseline: boolean
  baselineVerified: boolean
}

function compareQuery(params: CompareParams): string {
  return new URLSearchParams({
    doc_id: params.docId,
    version_a: params.versionA,
    version_b: params.versionB,
    receiver_id: params.receiverId,
    receiver_has_baseline: String(params.hasBaseline),
    baseline_verified: String(params.baselineVerified),
  }).toString()
}

export async function compareVersions(params: CompareParams): Promise<VersionDiffResult> {
  const response = await request(`/api/doc_version/compare?${compareQuery(params)}`)
  if (!response.ok) throw new Error('版本差异读取失败')
  const payload = await response.json()
  if (payload.error) throw new Error(payload.error)
  return payload
}

export async function deliverIncremental(
  params: CompareParams,
  options: { failAfterShards?: number } = {},
): Promise<DeliveryResult> {
  const response = await request('/api/result_deliver/incremental', {
    method: 'POST',
    body: JSON.stringify({
      doc_id: params.docId,
      base_version: params.versionA,
      target_version: params.versionB,
      receiver_id: params.receiverId,
      receiver_has_baseline: params.hasBaseline,
      baseline_verified: params.baselineVerified,
      fail_after_shards: options.failAfterShards ?? null,
    }),
  })
  if (!response.ok) throw new Error('增量交付失败')
  return await response.json()
}

export async function resumeDelivery(requestKey: string): Promise<DeliveryResult> {
  const response = await request('/api/result_deliver/resume', {
    method: 'POST',
    body: JSON.stringify({ request_key: requestKey }),
  })
  if (!response.ok) throw new Error('交付续传失败')
  return await response.json()
}

export async function getReceipt(requestKey: string): Promise<DeliveryReceipt> {
  const response = await request(
    `/api/result_deliver/receipt?request_key=${encodeURIComponent(requestKey)}`,
  )
  if (!response.ok) throw new Error('回执读取失败')
  const payload = await response.json()
  if (!payload.ok) throw new Error(payload.error ?? '回执读取失败')
  return payload.receipt
}
