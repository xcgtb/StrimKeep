<script setup lang="ts">
import { ref, computed } from 'vue';
import { readData, writeData, query, records, object, strings, type Data } from '../api/domain';
import { request } from '../api/client';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import PageHeading from '../components/PageHeading.vue';
import OperationState from '../components/OperationState.vue';
import ModalDialog from '../components/ModalDialog.vue';

const browse = useRequest<Data>(),
  scan = useRequest<Data>(),
  residue = useRequest<Data>(),
  op = useOperation();
const path = ref('');
const scope = ref('all'),
  limit = ref(100);
const pickerOpen = ref(false),
  browseSearch = ref(''),
  browseOffset = ref(0);
const residueOpen = ref(false),
  residueItem = ref<Data>(),
  residueResult = ref<Data>();
const cleanResult = ref<Data>(),
  cleanResultOpen = ref(false);

const preview = computed(() => records(scan.data.value?.preview));
const hits = computed(() => Number(scan.data.value?.hits ?? 0));
const scopes = computed(() => records(scan.data.value?.scopes));
const cleanErrors = computed(() => records(cleanResult.value?.errors));
const residueFiles = computed(() => strings(residue.data.value?.files));
const residueResultErrors = computed(() => records(residueResult.value?.errors));
const allRootsAvailable = computed(() => {
  const roots = records(browse.data.value?.roots);
  return roots.length === 2 && roots.every((r) => r.available !== false);
});

function invalidatePreview() {
  scan.cancel();
  scan.data.value = undefined;
  cleanResult.value = undefined;
}
function loadBrowse(next = '', offset = 0, search = '') {
  browseSearch.value = search;
  browseOffset.value = offset;
  return browse.run((signal) =>
    readData(
      query('/api/wash/empty-dirs/browse', {
        path: next,
        search,
        offset,
        limit: 200,
      }),
      signal,
    ),
  );
}
function browsePrev() {
  const r = object(browse.data.value);
  loadBrowse(
    String(r.path || ''),
    Math.max(0, Number(r.offset || 0) - Number(r.limit || 200)),
    browseSearch.value,
  );
}
function browseNext() {
  const r = object(browse.data.value);
  loadBrowse(
    String(r.path || ''),
    Number(r.offset || 0) + Number(r.limit || 200),
    browseSearch.value,
  );
}
function browseFilter() {
  loadBrowse(String(object(browse.data.value).path || ''), 0, browseSearch.value);
}
function openPicker() {
  pickerOpen.value = true;
  loadBrowse(path.value, 0, '');
}
function useSelection() {
  const r = object(browse.data.value);
  const p = String(r.path || '');
  scope.value = p ? 'directory' : 'all';
  path.value = p;
  pickerOpen.value = false;
  invalidatePreview();
}
async function scanEmpty() {
  invalidatePreview();
  await scan.run((signal) =>
    request<Data>('/api/wash/empty-dirs/scan', {
      method: 'POST',
      signal,
      timeoutMs: 120000,
      body: { path: scope.value === 'all' ? '' : path.value, scope: scope.value, limit: limit.value },
    }),
  );
}
async function cleanEmpty() {
  const items = preview.value.map((r) => ({
    path: r.path,
    lib: r.lib,
    cloud_path: r.cloud_path || '',
  }));
  const r = await op.run(() => writeData('/api/wash/empty-dirs/clean', { items }));
  if (r) {
    cleanResult.value = r;
    cleanResultOpen.value = true;
    invalidatePreview();
  }
}
async function openResidue(item: Data) {
  residueItem.value = item;
  residueResult.value = undefined;
  residue.data.value = undefined;
  residueOpen.value = true;
  await residue.run((signal) =>
    request<Data>('/api/wash/cloud-residue/preview', {
      method: 'POST',
      signal,
      timeoutMs: 120000,
      body: { local_path: item.path },
    }),
  );
}
async function cleanResidue() {
  const r = await op.run(() =>
    writeData('/api/wash/cloud-residue/clean', {
      token: residue.data.value?.token,
      confirmed: true,
    }),
  );
  if (r) {
    residueResult.value = r;
    residue.data.value = undefined;
  }
}
function emptyDirFileSummary(x: Data) {
  const fc = Number(x.file_count ?? 0);
  if (!fc) return '目录内没有文件';
  const exts = object(x.extensions);
  const groups: Record<string, number> = { 字幕: 0, 图片: 0, NFO: 0, 其他附属文件: 0 };
  Object.entries(exts).forEach(([ext, n]) => {
    const e = ext.toLowerCase();
    const g = ['.srt', '.ass', '.ssa', '.sub', '.idx', '.sup'].includes(e)
      ? '字幕'
      : ['.jpg', '.jpeg', '.png', '.gif', '.webp'].includes(e)
        ? '图片'
        : e === '.nfo'
          ? 'NFO'
          : '其他附属文件';
    groups[g] += Number(n);
  });
  return (
    '附属文件 ' +
    fc +
    ' 个 · ' +
    Object.keys(groups)
      .filter((k) => groups[k])
      .map((k) => k + ' ' + groups[k] + ' 个')
      .join(' · ')
  );
}
</script>

<template>
  <PageHeading
    title="目录清理"
    subtitle="扫描只读 NAS；执行时联动清理 NAS 与对应 115 残留，不产生备份，失败逐项保留原因。"
  />

  <OperationState
    :busy="op.busy.value || scan.loading.value || residue.loading.value"
    :error="op.error.value || scan.error.value || browse.error.value || residue.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />

  <section class="panel">
    <h3 class="block-title">目录清理</h3>
    <p class="section-note">
      只扫描选中目录及其子目录，找出为空或只含 NFO、图片、字幕等已知附属文件的媒体目录（{tmdb-xxx}
      命名）。含 STRM、视频、未知文件或软链接的目录会保留；读取失败时扫描会报错。执行清理会一起删除
      NAS 和对应 115 的残留，不产生备份，并通知 Emby 刷新；115 有视频或清理失败时，跳过该项并显示原因。
    </p>
    <div class="action-row" style="margin-bottom: 10px">
      <button class="btn gray" :disabled="op.busy.value || scan.loading.value" @click="openPicker">
        更换扫描范围
      </button>
      <button
        class="btn"
        :disabled="scan.loading.value || op.busy.value || (scope !== 'all' && !path)"
        @click="scanEmpty"
      >
        {{ scan.loading.value ? '扫描中…' : '预览扫描' }}
      </button>
      <button v-if="hits > 0" class="btn red" :disabled="op.busy.value" @click="cleanEmpty">
        执行清理
      </button>
    </div>
    <div class="selected-path">
      扫描范围：{{ scope === 'all' ? '整个媒体库（本地库＋分享库）' : path }}
    </div>
    <div class="task-status" role="status" aria-live="polite">
      {{
        scan.loading.value
          ? '正在扫描 NAS，请等待预览结果…'
          : hits
            ? '扫描完成：命中 ' + hits + ' 项，当前预览 ' + preview.length + ' 项。'
            : '点击「预览扫描」开始，更换范围只选择目录。'
      }}
    </div>

    <div v-if="scan.data.value" class="ing-sum">
      <span class="ing-chip">扫描文件夹 <b>{{ scan.data.value.folders ?? 0 }}</b></span>
      <span class="ing-chip">命中总数 <b>{{ hits }}</b></span>
      <span class="ing-chip">残留目录命中 <b>{{ scan.data.value.empty_dirs ?? 0 }}</b></span>
      <span class="ing-chip">预览上限 <b>{{ scan.data.value.preview_limit ?? 100 }}</b></span>
    </div>
    <div v-if="scopes.length" class="ing-sum">
      <span v-for="s in scopes" :key="String(s.lib)" class="ing-chip">
        {{ s.lib === 'share' ? '分享库' : '本地库' }}命中 <b>{{ s.hits ?? 0 }}</b>
      </span>
    </div>

    <div>
      <div v-for="(x, i) in preview" :key="i" class="ing-grp">
        <div class="ing-body">
          <div class="ing-head">
            <b>{{ i + 1 }}</b> · <span class="ing-src">{{ x.lib === 'share' ? '分享库' : '本地库' }}</span>
            <span class="rel">{{ x.rel }}</span>
          </div>
          <div class="dim">类型：{{ x.type }} · NAS：{{ x.abs_path }}<span v-if="x.local_present === false">（已不存在）</span></div>
          <div v-if="x.cloud_path" class="dim">
            115：{{ x.cloud_path }}<span v-if="x.cloud_present === false">（无对应目录）</span>
          </div>
          <div v-if="x.cloud_reason" class="warn">115 保留原因：{{ x.cloud_reason }}</div>
          <div v-if="x.nas_file_count != null" class="dim">NAS 附属文件 {{ x.nas_file_count }} 个</div>
          <div class="dim">内容：{{ emptyDirFileSummary(x) }}</div>
          <div class="dim">规则：{{ x.rule }}</div>
          <button
            v-if="x.lib === 'local'"
            class="btn sm gray"
            style="margin-top: 8px"
            :disabled="residue.loading.value"
            @click="openResidue(x)"
          >
            查看对应 115 残留
          </button>
        </div>
      </div>
      <div v-if="!preview.length && !scan.loading.value" class="list-empty">
        选择整个媒体库、单个库或子目录后预览扫描
      </div>
    </div>
  </section>

  <ModalDialog v-if="pickerOpen" title="选择扫描范围" @close="pickerOpen = false">
    <p class="dim" style="margin-bottom: 12px">可扫描本地与分享两库，或选择单个库及其子目录。</p>
    <p v-if="browse.error.value" class="inline-error">{{ browse.error.value }}</p>
    <div class="dir-picker-row">
      <button
        class="btn"
        :class="!object(browse.data.value).path ? '' : 'gray'"
        :disabled="!allRootsAvailable"
        @click="loadBrowse('')"
      >
        整个媒体库
      </button>
      <button
        v-for="root in records(browse.data.value?.roots)"
        :key="String(root.path)"
        class="btn"
        :class="root.lib === object(browse.data.value).lib ? '' : 'gray'"
        :disabled="root.available === false"
        @click="loadBrowse(String(root.path))"
      >
        {{ root.name }}<span v-if="root.available === false">（不可访问）</span>
      </button>
    </div>
    <template v-if="object(browse.data.value).path">
      <div class="crumbs">
        <span class="dim">当前位置：</span>
        <button class="btn sm gray" @click="loadBrowse(String(records(browse.data.value?.breadcrumbs)[0]?.path || ''))">
          库根目录
        </button>
        <template v-for="(c, i) in records(browse.data.value?.breadcrumbs).slice(1)" :key="String(c.path)">
          <span aria-hidden="true">›</span>
          <button
            v-if="i < records(browse.data.value?.breadcrumbs).length - 2"
            class="btn sm gray"
            @click="loadBrowse(String(c.path))"
          >
            {{ c.name }}
          </button>
          <span v-else class="dim">{{ c.name }}</span>
        </template>
      </div>
      <div class="dim" style="overflow-wrap: anywhere">目录路径：{{ object(browse.data.value).path }}</div>
      <div class="dir-picker-row">
        <input
          v-model="browseSearch"
          placeholder="筛选当前层文件夹"
          @keydown.enter="browseFilter"
        />
        <button class="btn gray" @click="browseFilter">筛选</button>
      </div>
      <div>
        <button
          v-for="item in records(browse.data.value?.items)"
          :key="String(item.path)"
          class="dir-picker-item"
          @click="loadBrowse(String(item.path))"
        >
          <span>📁 {{ item.name }}</span><span aria-hidden="true">›</span>
        </button>
        <div v-if="!records(browse.data.value?.items).length" class="list-empty">
          {{ browseSearch ? '没有匹配的子目录，可清空筛选' : '没有子目录，可选择当前目录' }}
        </div>
      </div>
      <div v-if="Number(browse.data.value?.total)" class="dim" style="margin-top: 10px">
        显示 {{ Number(browse.data.value?.offset || 0) + 1 }}–{{ Number(browse.data.value?.offset || 0) + records(browse.data.value?.items).length }} /
        {{ browse.data.value?.total }} 个文件夹
      </div>
      <div v-if="Number(browse.data.value?.offset) || browse.data.value?.has_more" class="dir-picker-row">
        <button class="btn gray" :disabled="!Number(browse.data.value?.offset)" @click="browsePrev">上一页</button>
        <button class="btn gray" :disabled="!browse.data.value?.has_more" @click="browseNext">下一页</button>
      </div>
    </template>
    <p class="dim" style="margin: 10px 0">确认后返回页面，再点击「预览扫描」。</p>
    <div class="dir-picker-footer">
      <button class="btn gray" @click="pickerOpen = false">取消</button>
      <button
        class="btn"
        :disabled="!object(browse.data.value).path && !allRootsAvailable"
        @click="useSelection"
      >
        确认选择
      </button>
    </div>
  </ModalDialog>

  <ModalDialog v-if="residueOpen" title="115 对应目录残留" @close="residueOpen = false">
    <p v-if="residue.error.value" class="inline-error">{{ residue.error.value }}</p>
    <template v-else-if="residueResult">
      <p>
        {{ residueResult.status === 'partial' ? '已删除部分附属文件，以下项目未完成：' : '115 残留已清理，无备份。' }}
        共删除 {{ residueResult.files_removed ?? 0 }} 个附属文件。
      </p>
      <div v-if="residueResultErrors.length" class="err-list">
        <div v-for="(e, i) in residueResultErrors" :key="i">{{ e }}</div>
      </div>
      <p class="dim" style="margin-top: 10px">返回当前列表后，可继续执行本地残留清理。</p>
      <div class="dir-picker-footer"><button class="btn" @click="residueOpen = false">返回本地残留列表</button></div>
    </template>
    <template v-else-if="residue.data.value">
      <p class="dim">NAS 对应目录：{{ residue.data.value.local_path }}</p>
      <p class="dim">115 待清理目录：{{ residue.data.value.cloud_path }}</p>
      <p><b>{{ emptyDirFileSummary(residue.data.value) }}</b></p>
      <p class="dim">
        确认后直接删除此 115 残留，不产生备份；失败项目会跳过并显示原因。NAS 字幕保留。预览有效期 10 分钟。
      </p>
      <div class="path-list">
        <div v-for="(f, i) in residueFiles" :key="i">{{ f }}</div>
        <p v-if="residue.data.value.files_truncated" class="dim">仅显示前 50 个文件；上方数量包含全部文件。</p>
      </div>
      <div class="dir-picker-footer">
        <button class="btn gray" @click="residueOpen = false">关闭</button>
        <button class="btn red" :disabled="op.busy.value" @click="cleanResidue">清理此 115 残留</button>
      </div>
    </template>
    <div v-else class="list-empty">正在核对云端目录与附属文件内容…</div>
  </ModalDialog>

  <ModalDialog
    v-if="cleanResultOpen"
    :title="cleanErrors.length ? '清理结束：有未完成项目' : '清理完成'"
    @close="cleanResultOpen = false"
  >
    <p v-if="cleanResult?.completed_at" class="dim">{{ cleanResult.completed_at }}</p>
    <p>
      NAS：{{ cleanResult?.nas_count ?? 0 }} 个目录、{{ cleanResult?.files_removed ?? 0 }} 个文件；115：{{ cleanResult?.cloud_count ?? 0 }}
      个目录、{{ cleanResult?.cloud_files_removed ?? 0 }} 个文件
    </p>
    <p>
      未完成 {{ cleanResult?.failed_count ?? 0 }} 项<span v-if="cleanResult?.already_absent">
        · 已不存在 {{ cleanResult.already_absent }} 项</span
      >
    </p>
    <div v-if="cleanErrors.length" class="err-list">
      <div v-for="(e, i) in cleanErrors" :key="i">{{ e }}</div>
    </div>
    <p v-if="cleanResult?.audit_error" class="warn">{{ cleanResult.audit_error }}</p>
    <p class="dim" style="margin-top: 10px">详情已保存到计划存档。</p>
    <div class="dir-picker-footer"><button class="btn gray" @click="cleanResultOpen = false">关闭</button></div>
  </ModalDialog>
</template>

<style scoped>
.block-title {
  font-size: 15px;
  margin-bottom: 8px;
}
.section-note {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.8;
  margin: 8px 0 14px;
}
.selected-path {
  font-size: 12px;
  color: var(--text-dim);
  overflow-wrap: anywhere;
  margin-bottom: 12px;
}
.task-status {
  font-size: 13px;
  margin-bottom: 12px;
  color: var(--text-mid);
}
.dim {
  color: var(--text-dim);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.warn {
  color: var(--warn);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.ing-sum {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.ing-chip {
  background: var(--surface-solid);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 12px;
  color: var(--text-mid);
}
.ing-chip b {
  color: var(--brand);
  font-size: 14px;
  margin-right: 2px;
}
.ing-grp {
  background: var(--surface-solid);
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  margin-bottom: 8px;
  overflow: hidden;
}
.ing-body {
  padding: 10px 14px;
  font-size: 12px;
  line-height: 1.9;
}
.ing-head {
  font-size: 13px;
  font-weight: 600;
}
.ing-src {
  font-size: 11px;
  color: var(--info);
  background: var(--info-soft);
  border-radius: 6px;
  padding: 1px 8px;
  white-space: nowrap;
}
.rel {
  overflow-wrap: anywhere;
}
.dir-picker-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  margin: 12px 0;
}
.dir-picker-row input {
  flex: 1;
  min-width: 140px;
  padding: 9px 12px;
}
.dir-picker-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  width: 100%;
  padding: 12px;
  margin: 6px 0;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface-solid);
  color: var(--text);
  text-align: left;
  cursor: pointer;
  font-size: 13px;
}
.dir-picker-item span {
  min-width: 0;
  overflow-wrap: anywhere;
}
.dir-picker-item:hover {
  border-color: var(--brand);
}
.dir-picker-footer {
  position: sticky;
  bottom: -1px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  padding: 14px 0 2px;
  margin-top: 12px;
  background: var(--surface-solid);
  border-top: 1px solid var(--border);
}
.crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0;
}
.path-list {
  max-height: 240px;
  overflow: auto;
  font-size: 11px;
  line-height: 1.7;
  word-break: break-all;
  color: var(--text-mid);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 12px;
  margin: 8px 0;
}
.err-list {
  max-height: 240px;
  overflow: auto;
  font-size: 12px;
  line-height: 1.8;
  overflow-wrap: anywhere;
  color: var(--err);
}
</style>
