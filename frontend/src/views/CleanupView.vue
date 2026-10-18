<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue';
import { readData, writeData, query, records, type Data } from '../api/domain';
import { request } from '../api/client';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
const browse = useRequest<Data>(),
  scan = useRequest<Data>(),
  op = useOperation(),
  tab = ref('directories'),
  path = ref(''),
  // 默认「整个本地与分享库」：1.2.12 的默认扫描范围就是整个媒体库，
  // 且这样首屏按钮可用，不会因为没选目录而呈现为不可点击的死按钮。
  scope = ref('all'),
  limit = ref(100),
  depth = ref(5),
  selected = ref(new Set<string>()),
  confirm = ref(false),
  validated = ref<Data>(),
  browseSearch = ref(''),
  browseOffset = ref(0);
const candidates = computed(() =>
  records(tab.value === 'orphans' ? scan.data.value?.orphan_dirs : scan.data.value?.preview),
);
function invalidate() {
  scan.cancel();
  scan.data.value = undefined;
  validated.value = undefined;
  selected.value.clear();
  confirm.value = false;
}
watch([tab, path, scope, limit, depth], invalidate);
function loadBrowse(next = '') {
  browseOffset.value = 0;
  return browse.run((signal) =>
    readData(
      query('/api/wash/empty-dirs/browse', {
        path: next,
        search: browseSearch.value,
        offset: 0,
        limit: 200,
      }),
      signal,
    ),
  );
}
async function loadMore() {
  const p = String(browse.data.value?.path || '');
  browseOffset.value += 200;
  const old = records(browse.data.value?.items);
  const r = await browse.run((signal) =>
    readData(
      query('/api/wash/empty-dirs/browse', {
        path: p,
        search: browseSearch.value,
        offset: browseOffset.value,
        limit: 200,
      }),
      signal,
    ),
  );
  if (r) browse.data.value = { ...r, items: [...old, ...records(r.items)] };
}
async function loadScan() {
  validated.value = undefined;
  selected.value.clear();
  await scan.run((signal) =>
    tab.value === 'orphans'
      ? readData(query('/api/orphans', { max_depth: depth.value }), signal)
      : request<Data>(
          tab.value === 'residue' ? '/api/wash/cloud-residue/preview' : '/api/wash/empty-dirs/scan',
          {
            method: 'POST',
            signal,
            timeoutMs: 120000,
            body:
              tab.value === 'residue'
                ? { local_path: path.value }
                : {
                    path: scope.value === 'all' ? '' : path.value,
                    scope: scope.value,
                    limit: limit.value,
                  },
          },
        ),
  );
}
function toggle(p: string) {
  validated.value = undefined;
  const next = new Set(selected.value);
  next.has(p) ? next.delete(p) : next.add(p);
  selected.value = next;
}
// 选中某个目录后自动切到「所选目录」范围，避免用户以为扫描了整个库。
function useCurrentDirectory() {
  path.value = String(browse.data.value?.path || '');
  if (path.value) scope.value = 'directory';
}
watch(scope, (value) => {
  if (value === 'all') path.value = '';
});
async function previewClean() {
  if (tab.value === 'orphans') {
    const r = await op.run(() =>
      writeData('/api/orphans/clean', { paths: [...selected.value], dry_run: true }),
    );
    if (!r) return;
    validated.value = r;
  } else validated.value = scan.data.value;
  confirm.value = true;
}
async function execute() {
  let endpoint = '/api/wash/empty-dirs/clean',
    body: Data = { items: candidates.value.filter((r) => selected.value.has(String(r.path))) };
  if (tab.value === 'orphans') {
    endpoint = '/api/orphans/clean';
    body = { paths: [...selected.value], dry_run: false };
  } else if (tab.value === 'residue') {
    endpoint = '/api/wash/cloud-residue/clean';
    body = { token: scan.data.value?.token, confirmed: true };
  }
  const r = await op.run(() => writeData(endpoint, body));
  if (r) {
    confirm.value = false;
    validated.value = undefined;
    scan.data.value = undefined;
    selected.value.clear();
  }
}
onMounted(() => loadBrowse());
</script>
<template>
  <PageHeading
    title="目录与残留清理"
    subtitle="扫描、选择、核对影响，再执行清理；逐项保留实际失败原因。"
  />
  <nav class="browse-tabs">
    <button
      v-for="(label, key) in {
        directories: '空目录与附属文件',
        orphans: '孤儿与已确认洗版残留',
        residue: '对应网盘残留',
      }"
      :key="key"
      :class="{ active: tab === key }"
      @click="tab = key"
    >
      {{ label }}
    </button>
  </nav>
  <OperationState
    :busy="op.busy.value || scan.loading.value"
    :error="op.error.value || scan.error.value || browse.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <section class="panel">
    <div v-if="tab === 'directories'" class="cleanup-scope">
      <h2>扫描范围</h2>
      <div class="action-row">
        <button class="button" :class="{ active: scope === 'all' }" @click="scope = 'all'">
          整个媒体库
        </button>
        <button
          v-for="root in records(browse.data.value?.roots)"
          :key="String(root.path)"
          class="button"
          :class="{ active: scope === 'directory' && path === root.path }"
          :disabled="root.available === false"
          @click="
            path = String(root.path);
            scope = 'directory';
            loadBrowse(String(root.path));
          "
        >
          {{ root.name }}
        </button>
      </div>
      <p class="muted-text">
        {{
          scope === 'all' ? '整个本地库与分享库' : path || '请选择媒体目录'
        }}；点击「扫描并预览」后才读取清理候选。
      </p>
    </div>
    <div class="two-column">
      <div>
        <h2>选择 NAS 媒体目录</h2>
        <nav class="file-breadcrumb">
          <button @click="loadBrowse('')">库根 /</button
          ><button
            v-for="c in records(browse.data.value?.breadcrumbs)"
            :key="String(c.path)"
            @click="loadBrowse(String(c.path))"
          >
            {{ c.name }} /
          </button>
        </nav>
        <div class="directory-list">
          <button
            v-for="r in records(browse.data.value?.roots)"
            :key="String(r.path)"
            :disabled="r.available === false"
            @click="loadBrowse(String(r.path))"
          >
            📁 {{ r.name }} <small v-if="r.message">{{ r.message }}</small></button
          ><button
            v-for="r in records(browse.data.value?.items)"
            :key="String(r.path)"
            @click="loadBrowse(String(r.path))"
          >
            📁 {{ r.name }} →
          </button>
        </div>
        <button v-if="browse.data.value?.has_more" class="button small" @click="loadMore">
          更多目录</button
        ><button class="button" :disabled="!browse.data.value?.path" @click="useCurrentDirectory">
          使用当前目录
        </button>
      </div>
      <div class="form-stack">
        <label
          >选中目录<input
            v-model="path"
            :disabled="tab === 'directories' && scope === 'all'"
            :placeholder="
              tab === 'directories' && scope === 'all'
                ? '整个库扫描无需指定目录'
                : '选择媒体库中的绝对目录路径'
            " /></label
        ><label v-if="tab === 'directories'"
          >范围<select v-model="scope">
            <option value="directory">所选目录</option>
            <option value="all">整个本地与分享库</option>
          </select></label
        ><label v-if="tab === 'directories'"
          >预览上限<input v-model.number="limit" type="number" min="1" max="1000" /></label
        ><label v-if="tab === 'orphans'"
          >扫描深度<input v-model.number="depth" type="number" min="1" max="10" /></label
        ><button
          class="button primary"
          :disabled="
            scan.loading.value ||
            op.busy.value ||
            op.uncertain.value ||
            (tab !== 'orphans' && scope !== 'all' && !path) ||
            (tab === 'residue' && !path)
          "
          @click="loadScan"
        >
          {{ scan.loading.value ? '正在扫描…' : '扫描并预览' }}
        </button>
        <p v-if="scan.loading.value" class="scan-hint" role="status">
          正在读取目录，媒体库较大时可能需要几十秒，请勿重复点击。
        </p>
        <p v-else class="scan-hint">
          扫描范围：{{
            tab === 'orphans'
              ? `目录树深度 ${depth} 层`
              : tab === 'residue'
                ? path || '请先选择网盘对应目录'
                : scope === 'all'
                  ? '整个本地库与分享库'
                  : path || '请先选择目录'
          }}
        </p>
      </div>
    </div>
  </section>
  <section v-if="scan.data.value" class="panel">
    <header class="section-heading">
      <div>
        <h2>扫描结果</h2>
        <p class="muted-text">
          共命中 <b>{{ candidates.length }}</b> 项<span v-if="tab === 'directories'"
            >（预览上限 {{ limit }}）</span
          >；勾选后再核对影响，才会进入清理。
        </p>
      </div>
      <span class="state-pill" :class="{ success: candidates.length }">{{
        candidates.length ? '可核对清理' : '未发现可清理项'
      }}</span>
    </header>
    <template v-if="tab !== 'residue'"
      ><div class="selection-toolbar">
        <span>已选 {{ selected.size }} 项</span
        ><button
          class="text-link"
          @click="
            validated = undefined;
            selected = new Set(candidates.map((r) => String(r.path)));
          "
        >
          选择预览项目</button
        ><button
          class="text-link"
          @click="
            validated = undefined;
            selected.clear();
          "
        >
          清空
        </button>
      </div>
      <p v-if="!candidates.length" class="empty-result">
        本次扫描没有发现空目录、附属文件或残留。若确认这里有内容，请检查扫描范围是否选对，或换个目录再试。
      </p>
      <div v-else class="plan-item-list">
        <article v-for="(item, i) in candidates" :key="i">
          <label class="checkbox-label"
            ><input
              type="checkbox"
              :checked="selected.has(String(item.path))"
              @change="toggle(String(item.path))"
            /><span class="path-text">{{ item.path }}</span></label
          >
          <div>
            <p>{{ item.rule || item.reason || item.type }}</p>
            <small>{{ item.lib === 'local' ? '本地库及对应源残留' : '分享库 NAS' }}</small>
          </div>
        </article>
      </div>
      <details>
        <summary>完整扫描证据与未知文件（未知文件只报告）</summary>
        <EvidenceDetails :value="scan.data.value" /></details></template
    ><EvidenceDetails v-else :value="scan.data.value" /><button
      class="button danger-text"
      :disabled="op.busy.value || op.uncertain.value || (tab !== 'residue' && !selected.size)"
      @click="previewClean"
    >
      核对所选清理范围
    </button>
  </section>
  <ConfirmAction
    v-if="confirm && validated"
    title="确认目录残留清理"
    description="按预览直接删除所选附属文件与目录，不创建备份。本地库目录清理按原逻辑先核对并处理对应网盘残留，失败保留 NAS；分享库仅处理 NAS。未知媒体文件不会凭猜测删除。"
    phrase="确认清理所选目录"
    danger
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirm = false"
    @confirm="execute"
    ><EvidenceDetails
      :value="
        tab === 'residue'
          ? validated
          : tab === 'orphans'
            ? validated
            : candidates.filter((r) => selected.has(String(r.path)))
      "
  /></ConfirmAction>
</template>
