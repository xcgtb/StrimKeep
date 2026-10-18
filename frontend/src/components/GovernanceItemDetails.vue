<script setup lang="ts">
import { computed } from 'vue';
import { object, records, strings, type Data } from '../api/domain';
import EvidenceDetails from './EvidenceDetails.vue';
const props = defineProps<{ item: Data; kind?: string; archived?: boolean }>();
const meta = computed(() => object(props.item.meta));
const compare = computed(() => object(meta.value.compare));
const kind = computed(() => String(props.item.kind || props.kind || ''));
const rows = computed(() =>
  [
    [
      '原因',
      props.item.reason_label || meta.value.reason_label || props.item.detail || props.item.reason,
    ],
    ['作品', meta.value.title],
    [
      '季',
      meta.value.season == null ? undefined : `S${String(meta.value.season).padStart(2, '0')}`,
    ],
    ['匹配来源', meta.value.match_basis],
    ['匹配依据', meta.value.match_basis_detail],
    [
      '本地 / 分享季数',
      meta.value.local_seasons == null && meta.value.share_seasons == null
        ? undefined
        : `${meta.value.local_seasons ?? '—'} / ${meta.value.share_seasons ?? '—'}`,
    ],
    [
      '本地 / 分享集数',
      meta.value.local_total == null && meta.value.share_total == null
        ? undefined
        : `${meta.value.local_total ?? '—'} / ${meta.value.share_total ?? '—'}`,
    ],
    ['缺失集数', strings(meta.value.missing_eps).join('、')],
    ['命中关键词', strings(meta.value.keywords).join('、')],
    ['TMDB', strings(meta.value.tmdb).join(' / ')],
    ['本地 TMDB', strings(meta.value.local_tmdb).join(' / ')],
    ['分享 TMDB', strings(meta.value.share_tmdb).join(' / ')],
    ['涉及文件数', props.item.files_count ?? strings(props.item.files).length],
  ].filter(([, value]) => value !== undefined && value !== null && value !== ''),
);
const deleteLabel = computed(() => (props.archived ? '计划删除' : '将删除'));
const pathGroups = computed(() =>
  [
    {
      label: '本地库',
      paths: strings(meta.value.local_paths),
      count: meta.value.local_count,
      action: kind.value === 'loc' ? '本地对照' : '保留依据',
    },
    {
      label: '分享库',
      paths: strings(meta.value.share_paths),
      count: meta.value.share_count,
      action: kind.value === 'shr' ? '分享对照' : '保留依据',
    },
    {
      label: '本次计划删除的文件',
      paths: strings(props.item.files),
      count: props.item.files_count,
      action: deleteLabel.value,
    },
  ].filter((g) => g.paths.length),
);
const verdicts: Record<string, string> = {
  a: '分享胜',
  b: '本地胜',
  tie: '打平',
  skip: '已关闭',
  after: '前维已决胜',
};
</script>
<template>
  <div class="governance-detail">
    <h3>{{ item.text || item.title || item.name }}</h3>
    <p v-if="item.detail" class="notice-bar">{{ item.detail }}</p>
    <dl class="business-facts">
      <div v-for="[label, value] in rows" :key="String(label)">
        <dt>{{ label }}</dt>
        <dd>{{ value }}</dd>
      </div>
    </dl>
    <p v-if="meta.tmdb_consistent === false" class="notice-bar warning-text">
      双方 TMDB 标识不一致，请核对作品身份。
    </p>
    <section v-if="records(compare.dims).length" class="governance-comparison">
      <h3>
        七维画质对比依据<span v-if="compare.episode != null">
          · 代表集 E{{ String(compare.episode).padStart(2, '0') }}</span
        >
      </h3>
      <p class="muted-text">
        {{
          compare.result === 0
            ? '画质打平，按已保存的平局保留规则决策'
            : Number(compare.result) > 0
              ? '分享版画质更优'
              : '本地版画质更优'
        }}
      </p>
      <p class="path-box">
        分享：{{ compare.share_name || '—' }}<br />本地：{{ compare.local_name || '—' }}
      </p>
      <div class="comparison-table">
        <table>
          <thead>
            <tr>
              <th>维度</th>
              <th>分享版</th>
              <th>本地版</th>
              <th>结果 / 原因</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="dim in records(compare.dims)"
              :key="String(dim.key)"
              :class="{ decisive: dim.key === compare.decided_by }"
            >
              <th>{{ dim.label }}</th>
              <td>{{ dim.a }}</td>
              <td>{{ dim.b }}</td>
              <td>
                {{ verdicts[String(dim.verdict)] || dim.verdict }}<small>{{ dim.reason }}</small>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <details v-for="group in pathGroups" :key="group.label" class="governance-paths">
      <summary>
        <strong>{{ group.label }}</strong
        ><span>{{ group.count ?? group.paths.length }} 个文件</span
        ><b :class="group.action === deleteLabel ? 'danger-text' : 'success-text'">{{
          group.action
        }}</b>
      </summary>
      <p v-for="path in group.paths" :key="path" class="path-box">
        <b v-if="strings(item.files).includes(path)" class="danger-text">{{ deleteLabel }} · </b
        >{{ path }}
      </p>
      <p v-if="Number(group.count) > group.paths.length" class="muted-text">
        仅列出前 {{ group.paths.length }} 个路径，共 {{ group.count }} 个文件。
      </p>
    </details>
    <section v-if="records(meta.cloud_sources).length">
      <h3>115 源文件联动</h3>
      <p v-for="(source, i) in records(meta.cloud_sources)" :key="i" class="muted-text">
        {{ source.blocked || source.effect }} ·
        {{ source.enabled ? '接口已就绪' : '删除接口未就绪' }} ·
        {{ source.file_id || '文件待核实' }}
      </p>
    </section>
    <details class="technical-details">
      <summary>完整核验明细</summary>
      <EvidenceDetails :value="item" />
    </details>
  </div>
</template>
