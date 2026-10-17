<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { get } from '../api/client';
import { date, object, records, text } from '../api/domain';

const plans = ref<Record<string, unknown>[]>([]);
const archives = ref<Record<string, unknown>[]>([]);
const limit = ref(20);
const loading = ref(false);
const error = ref('');
const detail = ref<Record<string, unknown> | null>(null);
const stateLabels: Record<string,string> = { pending: '待执行', done: '已执行', executed: '已执行', expired: '已失效', failed: '执行失败' };
function stateLabel(v: unknown) { const k = text(v); return stateLabels[k] || k || '未知状态'; }

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = object(await get<Record<string, unknown>>(`/api/plans?limit=${limit.value}`));
    plans.value = records(data.plans);
    archives.value = records(data.archives);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
async function openPlan(id: string) {
  try {
    const data = object(await get<Record<string, unknown>>(`/api/plan/${encodeURIComponent(id)}`));
    detail.value = object(data.plan || data);
  } catch (e) {
    error.value = (e as Error).message;
  }
}
onMounted(load);
</script>
<template>
  <section class="tab active">
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="bookmark">计划存档</h3>
        <div style="display: flex; gap: 8px; align-items: center">
          <input v-model.number="limit" type="number" min="1" max="100" style="width: 70px; padding: 6px 10px" />
          <button class="btn gray" data-icon="refresh" @click="load">刷新</button>
        </div>
      </div>
      <p class="section-note">
        治理计划与清理结果集中保存在这里。待执行计划 2 小时内有效；清理结果和已执行计划长期保留，可回看具体明细。
      </p>
      <p v-if="error" class="section-note">{{ error }}</p>
    </div>

    <div class="panel">
      <div class="plan-row list-empty" v-if="!plans.length">{{ loading ? '加载中…' : '暂无治理计划' }}</div>
      <div v-for="plan in plans" :key="text(plan.id)" class="plan-row" style="padding:8px 0;border-bottom:1px solid var(--border);cursor:pointer" @click="openPlan(text(plan.id))">
        <div style="font-size: 13px; font-weight: 600">
          {{ date(Number(plan.ts)) }}
          <span style="font-weight: 400; color: var(--text-mid)"> · {{ stateLabel(plan.state) }}</span>
        </div>
        <div style="color: var(--text-mid); font-size: 12px; margin-top: 2px">
          本地 {{ text(object(plan.stats).loc ?? '—') }} / 分享 {{ text(object(plan.stats).shr ?? '—') }}
        </div>
      </div>
    </div>

    <div v-if="detail" class="panel">
      <div class="row-between">
        <h3>计划详情 · {{ text(detail.id) }}</h3>
        <button class="btn gray sm" @click="detail = null">关闭</button>
      </div>
      <div class="meta-row"><span class="k">状态</span><span class="v">{{ stateLabel(detail.state) }}</span></div>
      <div class="meta-row"><span class="k">生成时间</span><span class="v">{{ date(Number(detail.ts)) }}</span></div>
      <div class="meta-row"><span class="k">本地清理</span><span class="v">{{ text(object(detail.stats).loc ?? '—') }}</span></div>
      <div class="meta-row"><span class="k">分享清理</span><span class="v">{{ text(object(detail.stats).shr ?? '—') }}</span></div>
    </div>

    <div class="panel">
      <h3>治理与目录清理结果</h3>
      <div v-if="!archives.length" class="list-empty">{{ loading ? '加载中…' : '暂无执行归档' }}</div>
      <div v-for="row in archives" :key="text(row.id)" class="plan-row" style="padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="font-size: 13px; font-weight: 600">{{ text(row.title) || text(row.category) }}</div>
        <div style="color: var(--text-mid); font-size: 12px; margin-top: 2px">
          {{ date(Number(row.ts)) }} · {{ text(row.category) }}
        </div>
      </div>
    </div>
  </section>
</template>
