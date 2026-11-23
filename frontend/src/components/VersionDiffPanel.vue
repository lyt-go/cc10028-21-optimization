<template>
  <section class="version-panel" data-testid="version-diff-panel">
    <header class="panel-head">
      <div>
        <h3>{{ title }}</h3>
        <p class="page-desc">{{ subtitle }}</p>
      </div>
      <span v-if="diff" class="mode-badge" :class="`mode-${diff.mode}`">
        {{ modeLabel(diff.mode) }}
      </span>
    </header>

    <form class="filter-bar" @submit.prevent="runCompare">
      <label class="filter-item">
        <span>文档编号</span>
        <input v-model="form.docId" data-testid="diff-doc" placeholder="如 DOC-0001" />
      </label>
      <label class="filter-item">
        <span>基准版本</span>
        <input v-model="form.versionA" data-testid="diff-version-a" placeholder="v1.0" />
      </label>
      <label class="filter-item">
        <span>目标版本</span>
        <input v-model="form.versionB" data-testid="diff-version-b" placeholder="v2.0" />
      </label>
      <label class="filter-item">
        <span>接收方</span>
        <input v-model="form.receiverId" data-testid="diff-receiver" placeholder="receiver-01" />
      </label>
      <label class="filter-item checkbox">
        <input v-model="form.hasBaseline" data-testid="diff-has-baseline" type="checkbox" />
        <span>接收方持有基准</span>
      </label>
      <label class="filter-item checkbox">
        <input
          v-model="form.baselineVerified"
          data-testid="diff-baseline-verified"
          type="checkbox"
          :disabled="!form.hasBaseline"
        />
        <span>基准可核实</span>
      </label>
      <button class="btn primary" data-testid="diff-run" type="submit" :disabled="loading">
        {{ loading ? '查询中…' : '检索版本差异' }}
      </button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="diff-error">{{ errorMessage }}</p>

    <template v-if="diff">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">实体数（基准 → 目标）</span>
          <strong class="stat-value">
            {{ diff.counts.base.entities }} → {{ diff.counts.target.entities }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">关系数（基准 → 目标）</span>
          <strong class="stat-value">
            {{ diff.counts.base.relations }} → {{ diff.counts.target.relations }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">变动条目</span>
          <strong class="stat-value">{{ diff.summary.total_entries }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">基准状态</span>
          <strong class="stat-value small">{{ baselineLabel(diff.baseline.status) }}</strong>
        </article>
      </div>

      <p v-if="diff.equal_count_data_change" class="warn-banner" data-testid="equal-count-warning">
        计数未变但数据发生变化：新增 {{ diff.summary.by_change.added }} 条、删除
        {{ diff.summary.by_change.removed }} 条、修改 {{ diff.summary.by_change.modified }} 条。
        仅比较数量的对比会漏掉这批变化。
      </p>
      <p v-else-if="!diff.record_diff_available" class="warn-banner" data-testid="record-diff-unavailable">
        {{ diff.message }}
      </p>

      <h4 class="section-title">版本差异选交清单</h4>
      <p v-if="diff.selection" class="page-desc">
        选交 {{ diff.selection.record_counts.entities }} 个实体、
        {{ diff.selection.record_counts.relations }} 条关系、
        {{ diff.selection.record_counts.tombstones }} 个删除墓碑、
        {{ diff.selection.record_counts.dependency_entities }} 个端点补全实体。
      </p>
      <ul v-if="diff.selection?.notes.length" class="note-list" data-testid="selection-notes">
        <li v-for="(note, index) in diff.selection.notes" :key="index">{{ note }}</li>
      </ul>

      <table v-if="diff.entries.length" class="data-table" data-testid="diff-entries">
        <thead>
          <tr>
            <th>对象</th>
            <th>变化</th>
            <th>身份键</th>
            <th>前版本记录身份</th>
            <th>后版本记录身份</th>
            <th>变更字段</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="entry in diff.entries"
            :key="entry.change_id"
            :data-testid="`diff-entry-${entry.change_id}`"
          >
            <td>{{ entry.object_type === 'entity' ? '实体' : '关系' }}</td>
            <td><span class="change-tag" :class="`change-${entry.change}`">{{ changeLabel(entry.change) }}</span></td>
            <td class="mono">{{ entry.natural_key }}</td>
            <td class="mono">
              {{ entry.before.exists ? `${entry.before.version} / ${entry.before.record_id ?? '—'}` : '—' }}
            </td>
            <td class="mono">
              {{ entry.after.exists ? `${entry.after.version} / ${entry.after.record_id ?? '—'}` : '—' }}
            </td>
            <td>{{ entry.changed_fields.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h4 class="section-title">关系端点依赖</h4>
      <p class="page-desc">
        依赖状态：included=随变更入包，required=基准不可用已补全，existing=基准已存在，missing=目标版本缺失。
      </p>
      <table v-if="diff.selection?.dependencies.length" class="data-table" data-testid="diff-dependencies">
        <thead>
          <tr>
            <th>端点实体</th>
            <th>角色</th>
            <th>所属关系</th>
            <th>状态</th>
            <th>所属版本</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(dep, index) in diff.selection.dependencies" :key="`${dep.relation_key}-${dep.role}-${index}`">
            <td>{{ dep.entity_text || dep.natural_key }}</td>
            <td>{{ dep.role === 'head' ? '头实体' : '尾实体' }}</td>
            <td class="mono">{{ dep.relation_key }}</td>
            <td><span class="dep-tag" :class="`dep-${dep.status}`">{{ dep.status }}</span></td>
            <td>{{ dep.version }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-hint">本次选交没有需要额外补齐的端点依赖。</p>

      <div class="page-actions panel-actions">
        <button class="btn primary" data-testid="deliver-incremental" type="button" :disabled="busy || !diff.selection" @click="deliver">
          生成增量数据包
        </button>
        <button
          v-if="canResume"
          class="btn"
          data-testid="resume-delivery"
          type="button"
          :disabled="busy"
          @click="resume"
        >
          续传同一次请求
        </button>
      </div>

      <p v-if="deliveryMessage" class="delivery-message" data-testid="delivery-message">{{ deliveryMessage }}</p>

      <template v-if="receipt">
        <h4 class="section-title">交付回执</h4>
        <dl class="receipt-grid" data-testid="delivery-receipt">
          <div><dt>回执编号</dt><dd class="mono">{{ receipt.回执编号 }}</dd></div>
          <div><dt>请求标识</dt><dd class="mono">{{ receipt.request_key }}</dd></div>
          <div><dt>状态</dt><dd>{{ receipt.status }}</dd></div>
          <div><dt>模式</dt><dd>{{ modeLabel(receipt.mode) }}</dd></div>
          <div><dt>基准 → 目标</dt><dd>{{ receipt.base_version }} → {{ receipt.target_version }}</dd></div>
          <div><dt>分片</dt><dd>{{ receipt.shards.completed }} / {{ receipt.shards.total }}</dd></div>
          <div><dt>实体 / 关系</dt><dd>{{ receipt.counts.entities }} / {{ receipt.counts.relations }}</dd></div>
          <div><dt>删除墓碑</dt><dd>{{ receipt.counts.tombstones }}</dd></div>
        </dl>

        <table v-if="receipt.dependencies.length" class="data-table" data-testid="receipt-dependencies">
          <thead>
            <tr><th>依赖对象</th><th>类型</th><th>角色</th><th>所属版本</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="(dep, index) in receipt.dependencies" :key="`rcpt-${index}`">
              <td>{{ dep.entity_text || dep.natural_key }}</td>
              <td>{{ dep.object_type === 'entity' ? '实体' : dep.object_type }}</td>
              <td>{{ dep.role === 'head' ? '头实体' : '尾实体' }}</td>
              <td>{{ dep.version }}</td>
              <td><span class="dep-tag" :class="`dep-${dep.status}`">{{ dep.status }}</span></td>
            </tr>
          </tbody>
        </table>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'

import { compareVersions, deliverIncremental, getReceipt, resumeDelivery } from '@/api/backend-service'
import type { DeliveryReceipt, DeliveryResult, VersionDiffResult } from '@/data/types'

const props = withDefaults(
  defineProps<{ title?: string; subtitle?: string }>(),
  {
    title: '版本差异检索与选交',
    subtitle: '比较两个版本快照的记录级差异，按接收方基准形成版本差异选交清单。',
  },
)
void props

const form = reactive({
  docId: '',
  versionA: 'v1.0',
  versionB: 'v2.0',
  receiverId: 'default',
  hasBaseline: true,
  baselineVerified: true,
})
const diff = ref<VersionDiffResult | null>(null)
const errorMessage = ref('')
const loading = ref(false)
const busy = ref(false)
const deliveryMessage = ref('')
const receipt = ref<DeliveryReceipt | null>(null)
const lastRequestKey = ref('')
const canResume = computed(() => Boolean(lastRequestKey.value) && receipt.value?.status === '失败')

async function runCompare(): Promise<void> {
  errorMessage.value = ''
  deliveryMessage.value = ''
  loading.value = true
  try {
    diff.value = await compareVersions(toParams())
    receipt.value = null
    lastRequestKey.value = ''
  } catch (error) {
    diff.value = null
    errorMessage.value = error instanceof Error ? error.message : '版本差异读取失败'
  } finally {
    loading.value = false
  }
}

function toParams() {
  return {
    docId: form.docId.trim(),
    versionA: form.versionA.trim(),
    versionB: form.versionB.trim(),
    receiverId: form.receiverId.trim() || 'default',
    hasBaseline: form.hasBaseline,
    baselineVerified: form.hasBaseline && form.baselineVerified,
  }
}

async function deliver(): Promise<void> {
  busy.value = true
  errorMessage.value = ''
  deliveryMessage.value = ''
  try {
    const result = await deliverIncremental(toParams())
    applyDelivery(result)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量交付失败'
  } finally {
    busy.value = false
  }
}

async function resume(): Promise<void> {
  if (!lastRequestKey.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    applyDelivery(await resumeDelivery(lastRequestKey.value))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付续传失败'
  } finally {
    busy.value = false
  }
}

function applyDelivery(result: DeliveryResult): void {
  lastRequestKey.value = result.request_key
  receipt.value = result.receipt ?? null
  if (result.ok) {
    deliveryMessage.value = result.duplicate_suppressed
      ? `同一次请求已交付，复用回执 ${result.request_key}`
      : `增量数据包已生成（${result.request_key}），共 ${result.total_shards ?? 0} 个分片`
  } else {
    deliveryMessage.value = `交付失败，可凭 ${result.request_key} 续传`
  }
}

function modeLabel(mode: string): string {
  if (mode === 'incremental') return '增量（基准可核实）'
  if (mode === 'closure') return '增量+端点补全（基准不可核实）'
  return '全量（首次下载）'
}

function baselineLabel(status: string): string {
  if (status === 'verified') return '可核实'
  if (status === 'unverified') return '不可核实'
  return '无基准'
}

function changeLabel(change: string): string {
  if (change === 'added') return '新增'
  if (change === 'removed') return '删除'
  return '修改'
}

defineExpose({ runCompare, deliver })
</script>

<style scoped>
.version-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  margin: 16px 0;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}
.panel-head h3 { margin: 0 0 4px; font-size: 15px; }
.mode-badge {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  white-space: nowrap;
}
.mode-incremental { background: #ecfdf3; border-color: #abefc6; color: #067647; }
.mode-closure { background: #fffaeb; border-color: #fedf89; color: #b54708; }
.mode-full { background: #eff8ff; border-color: #b2ddff; color: #175cd3; }
.checkbox { flex-direction: row; align-items: center; gap: 6px; }
.checkbox span { margin: 0; }
.stat-value.small { font-size: 15px; }
.warn-banner {
  background: #fffaeb;
  border: 1px solid #fedf89;
  color: #b54708;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
}
.section-title { margin: 18px 0 8px; font-size: 14px; }
.note-list { margin: 0 0 8px; padding-left: 18px; color: var(--muted); font-size: 12px; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
.change-tag, .dep-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
}
.change-added, .dep-required { background: #ecfdf3; color: #067647; border-color: #abefc6; }
.change-removed, .dep-missing { background: #fef3f2; color: #b42318; border-color: #fecdca; }
.change-modified { background: #fffaeb; color: #b54708; border-color: #fedf89; }
.dep-existing { background: #f8f9fc; color: #475467; }
.dep-included { background: #eff8ff; color: #175cd3; border-color: #b2ddff; }
.panel-actions { margin-top: 14px; }
.delivery-message { font-size: 13px; color: var(--muted); }
.receipt-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px 16px;
  margin: 0;
}
.receipt-grid dt { font-size: 12px; color: var(--muted); }
.receipt-grid dd { margin: 2px 0 0; font-size: 13px; }
.empty-hint { color: var(--muted); font-size: 12px; }
</style>
