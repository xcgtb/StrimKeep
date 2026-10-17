<script setup lang="ts">
import { computed, ref, watch, nextTick } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { request } from '../api/client';
import {
  readData,
  writeData,
  accountPath,
  object,
  records,
  query,
  uuid,
  type Data,
} from '../api/domain';
import { useCloud } from '../stores/cloud';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import AccountPicker from '../components/AccountPicker.vue';
import PageHeading from '../components/PageHeading.vue';
import DirectoryPicker from '../components/DirectoryPicker.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
import MediaOverride from '../components/MediaOverride.vue';
const route = useRoute(),
  router = useRouter(),
  cloud = useCloud(),
  op = useOperation(() => cloud.id),
  preview = useRequest<Data>(),
  offline = useRequest<Data>(),
  receiving = useRequest<Data>(),
  directoryConfig = useRequest<Data>();
const shareOnly = computed(() => route.path === '/resources/share'),
  tab = ref(
    ['batch', 'share', 'offline', 'bot'].includes(String(route.query.tab))
      ? String(route.query.tab)
      : 'batch',
  ),
  input = ref(''),
  link = ref(''),
  password = ref(''),
  organize = ref(false),
  generate = ref(true),
  target = ref(''),
  targetName = ref(''),
  archive = ref(''),
  cid = ref('0'),
  crumbs = ref([{ id: '0', name: '分享根目录' }]),
  sharePage = ref<Data>(),
  selected = ref(new Set<string>()),
  plan = ref<Data>(),
  job = ref(''),
  confirm = ref(false),
  directConfirm = ref(false),
  directRequestId = ref(uuid()),
  offlinePage = ref(1),
  overrides = ref<Record<string, Data>>({}),
  fixing = ref<{ id: string; name: string }>(),
  receivingForm = ref<Data>({ tg_enabled: false, organize_after_transfer: false });
watch(
  () => cloud.id,
  () => {
    target.value = '';
    targetName.value = '';
    archive.value = '';
    password.value = '';
    link.value = '';
    input.value = '';
    fixing.value = undefined;
  },
);
let generation = 0;
function invalidate() {
  generation++;
  preview.cancel();
  plan.value = undefined;
  confirm.value = false;
  directConfirm.value = false;
  directRequestId.value = uuid();
  op.reconcile();
}
watch([() => cloud.id, input, link, password, shareOnly], () => {
  invalidate();
  sharePage.value = undefined;
  selected.value.clear();
  crumbs.value = [{ id: '0', name: '分享根目录' }];
  cid.value = '0';
  job.value = '';
  overrides.value = {};
});

watch([organize, target, archive, generate], invalidate);
async function previewBatch() {
  const r = await preview.run((signal) =>
    request<Data>('/api/cloud/resources/preview', {
      method: 'POST',
      signal,
      body: { account_id: cloud.id, text: input.value, organize: organize.value },
    }),
  );
  if (r) plan.value = object(r.plan);
}
async function browse(nextCid = '0', name = '分享根目录', more = false) {
  invalidate();
  const g = generation;
  const pageOffset = more
    ? Number(sharePage.value?.offset || 0) + records(sharePage.value?.items).length
    : 0;
  selected.value = new Set();
  if (!more) sharePage.value = undefined;
  const r = await preview.run((signal) =>
    request<Data>('/api/cloud/shares/preview', {
      method: 'POST',
      signal,
      body: {
        account_id: cloud.id,
        link: link.value,
        password: password.value,
        cid: nextCid,
        offset: pageOffset,
      },
    }),
  );
  if (!r || g !== generation) return;
  sharePage.value = r;
  cid.value = nextCid;
  if (!more) {
    const index = crumbs.value.findIndex((c) => c.id === nextCid);
    crumbs.value =
      index >= 0 ? crumbs.value.slice(0, index + 1) : [...crumbs.value, { id: nextCid, name }];
  }
}
function toggle(id: string) {
  invalidate();
  const values = new Set(selected.value);
  values.has(id) ? values.delete(id) : values.add(id);
  selected.value = values;
}
async function planShare() {
  invalidate();
  if (!shareOnly.value) {
    plan.value = {
      kind: 'transfer',
      preview_id: sharePage.value?.preview_id,
      files: records(sharePage.value?.items).filter((r) => selected.value.has(String(r.id))),
      target_cid: target.value,
      organize_target: organize.value ? archive.value : null,
      generate: generate.value,
    };
    return;
  }
  const r = await preview.run((signal) =>
    request<Data>('/api/cloud/shares/strm/preview', {
      method: 'POST',
      signal,
      body: {
        preview_id: sharePage.value?.preview_id,
        file_ids: [...selected.value],
        overrides: overrides.value,
      },
    }),
  );
  if (r) plan.value = object(r.plan);
}
async function execute() {
  if (!plan.value) return;
  const original = plan.value;
  let path = '/api/cloud/resources/apply',
    body: Data = { plan_id: original.id, confirmed: true };
  if (shareOnly.value) path = '/api/cloud/shares/strm/apply';
  else if (tab.value === 'share') {
    path = '/api/cloud/transfers';
    body = {
      preview_id: original.preview_id,
      file_ids: [...selected.value],
      target_cid: original.target_cid,
      organize_target: original.organize_target,
      generate: original.generate,
      confirmed: true,
    };
  }
  const r = await op.run(() => writeData(path, body));
  if (r) {
    confirm.value = false;
    job.value = String(r.job_id);
    plan.value = undefined;
  }
}
async function loadOffline() {
  if (cloud.id)
    await offline.run((signal) =>
      readData(
        query('/api/cloud/resources/offline', { account_id: cloud.id, page: offlinePage.value }),
        signal,
      ),
    );
}
async function loadReceiving() {
  if (!cloud.id) return;
  const aid = cloud.id;
  const r = await receiving.run((signal) => readData(accountPath(aid, 'receiving'), signal));
  if (r && aid === cloud.id) {
    const c = object(r.config);
    receivingForm.value = {
      tg_enabled: !!c.tg_enabled,
      organize_after_transfer: !!c.organize_after_transfer,
    };
    organize.value = !!c.organize_after_transfer;
  }
}
async function saveReceiving() {
  const r = await op.run(() =>
    writeData(accountPath(cloud.id, 'receiving'), {
      config: receivingForm.value,
      expected_revision: receiving.data.value?.revision,
    }),
  );
  if (r) await loadReceiving();
}
const destinations = computed(() => {
  const saved = object(directoryConfig.data.value?.directories);
  return [
    ['transfer', '115 分享'],
    ['magnet', '磁力'],
    ['ed2k', 'ed2k'],
    ['instant', '秒传'],
  ].map(([role, name]) => ({
    role: role!,
    name: name!,
    directory: String(object(saved[role!]).name || '尚未配置'),
  }));
});
async function loadDirectories() {
  if (!cloud.id) return;
  const aid = cloud.id;
  const result = await directoryConfig.run((signal) =>
    readData(accountPath(aid, 'directories'), signal),
  );
  if (result && aid === cloud.id) {
    const saved = object(result.directories);
    target.value = String(object(saved.transfer).id || '');
    targetName.value = String(object(saved.transfer).name || '');
    archive.value = String(object(saved.target).id || '');
  }
}
async function submitDirect() {
  const aid = cloud.id;
  const result = await op.run(() =>
    writeData('/api/cloud/resources/receive', {
      account_id: aid,
      text: input.value,
      expected_revision: directoryConfig.data.value?.revision,
      request_id: directRequestId.value,
      organize: organize.value,
      confirmed: true,
    }),
  );
  if (result && aid === cloud.id) {
    directConfirm.value = false;
    plan.value = undefined;
    input.value = '';
    await nextTick();
    if (aid === cloud.id) job.value = String(result.job_id);
    directRequestId.value = uuid();
  }
}
watch(
  () => cloud.id,
  () => {
    receiving.cancel();
    offline.cancel();
    directoryConfig.cancel();
    offlinePage.value = 1;
    organize.value = false;
    void loadReceiving();
    void loadDirectories();
  },
  { immediate: true },
);
const validTabs = ['batch', 'share', 'offline', 'bot'];
function switchTab(value: string) {
  if (!validTabs.includes(value) || shareOnly.value) return;
  void router.replace({ path: '/resources/receive', query: { ...route.query, tab: value } });
}
watch(
  () => [cloud.id, route.path, route.query.tab],
  () => {
    const next = validTabs.includes(String(route.query.tab)) ? String(route.query.tab) : 'batch';
    if (tab.value !== next) {
      invalidate();
      tab.value = next;
      sharePage.value = undefined;
      selected.value = new Set();
      offlinePage.value = 1;
    }
    if (!shareOnly.value && next === 'offline') void loadOffline();
  },
  { immediate: true },
);
</script>
<template>
  <PageHeading
    :title="shareOnly ? '分享 STRM 生成与整理' : '链接与离线转存'"
    :subtitle="
      shareOnly
        ? '浏览分享，直接生成到分类目录，保留分享源文件。'
        : '接收 115 分享、磁力、ed2k 与秒传资源，核对目标后提交。'
    "
  /><AccountPicker />
  <div v-if="shareOnly" class="share-path-banner">
    <span class="path-banner-icon">↳</span>
    <div>
      <strong>直接归档到分享库</strong>
      <p>使用已保存的分类与命名规则，不再创建“分享-ID”外层目录。</p>
    </div>
    <code>share / 电影 / 作品 / 影片.strm</code>
  </div>
  <nav v-if="shareOnly" class="module-navigation" aria-label="分享库功能">
    <RouterLink to="/resources/share" class="current">分享生成</RouterLink>
    <RouterLink to="/organize/naming">命名模板</RouterLink>
    <RouterLink to="/settings/cloud?section=classification">分类规则</RouterLink>
    <RouterLink to="/settings/cloud?section=output">分享输出目录</RouterLink>
    <RouterLink to="/tasks/ingestion">入库核验</RouterLink>
  </nav>
  <nav v-if="!shareOnly" class="browse-tabs">
    <button
      v-for="(label, key) in {
        batch: '批量链接',
        share: '分享浏览与选择',
        offline: '离线链接与记录',
        bot: '机器人接收',
      }"
      :key="key"
      :class="{ active: tab === key }"
      @click="switchTab(key)"
    >
      {{ label }}
    </button>
  </nav>
  <OperationState
    :busy="op.busy.value || preview.loading.value"
    :error="op.error.value || preview.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <p v-if="op.uncertain.value" class="notice-bar">
    操作结果未知，请在任务中心核实原任务后重新读取页面。<RouterLink to="/tasks" class="text-link"
      >查看任务</RouterLink
    >
  </p>
  <template v-if="cloud.id"
    ><section v-if="!shareOnly && (tab === 'batch' || tab === 'offline')" class="panel">
      <h2>{{ tab === 'offline' ? '添加离线链接' : '添加资源链接' }}</h2>
      <div class="destination-strip" aria-label="已保存的接收目录">
        <span v-for="destination in destinations" :key="destination.role"
          ><small>{{ destination.name }}</small
          ><strong>{{ destination.directory }}</strong></span
        >
      </div>
      <p v-if="directoryConfig.error.value" class="inline-error">
        {{ directoryConfig.error.value }}
      </p>
      <form class="form-stack" @submit.prevent="previewBatch">
        <label
          >资源内容<textarea
            v-model="input"
            rows="7"
            maxlength="16000"
            placeholder="支持混合粘贴多条分享、magnet、ed2k 或 115:// 链接"
            required
          ></textarea></label
        ><label class="checkbox-label"
          ><input v-model="organize" type="checkbox" />接收后按已保存归档配置整理</label
        ><RouterLink to="/settings/accounts" class="text-link">核对各类接收目录 →</RouterLink>
        <div class="action-row">
          <button
            class="button"
            :disabled="preview.loading.value || op.busy.value || op.uncertain.value"
          >
            识别资源并预览
          </button>
          <button
            type="button"
            class="button primary"
            :disabled="
              !input.trim() ||
              !directoryConfig.data.value ||
              !!directoryConfig.error.value ||
              preview.loading.value ||
              op.busy.value ||
              op.uncertain.value
            "
            @click="directConfirm = true"
          >
            一键提交到已保存目录
          </button>
        </div>
        <p class="data-caption">
          115 分享、磁力、ed2k
          和秒传分别使用上方目录。开启自动整理后按已保存策略执行；离线下载完成后的接续请在自动整理中配置扫描计划。
        </p>
      </form>
    </section>
    <section v-if="shareOnly || tab === 'share'" class="panel">
      <h2>{{ shareOnly ? '浏览分享内容' : '浏览并选择转存内容' }}</h2>
      <form class="filter-panel" @submit.prevent="browse()">
        <label class="grow">115 分享链接<input v-model="link" required /></label
        ><label>提取码<input v-model="password" type="password" autocomplete="off" /></label
        ><button
          class="button primary"
          :disabled="preview.loading.value || op.busy.value || op.uncertain.value"
        >
          读取分享
        </button>
      </form>
      <template v-if="sharePage"
        ><nav class="file-breadcrumb">
          <button v-for="c in crumbs" :key="c.id" @click="browse(c.id, c.name)">
            {{ c.name }} /
          </button>
        </nav>
        <div class="selection-toolbar">
          <span>已选 {{ selected.size }} 项</span
          ><button
            class="text-link"
            @click="
              invalidate();
              selected = new Set(records(sharePage.items).map((r) => String(r.id)));
            "
          >
            选择本页</button
          ><button
            class="text-link"
            @click="
              invalidate();
              selected = new Set();
            "
          >
            清空
          </button>
        </div>
        <div class="resource-file-list">
          <article v-for="row in records(sharePage.items)" :key="String(row.id)">
            <label class="checkbox-label"
              ><input
                type="checkbox"
                :checked="selected.has(String(row.id))"
                :aria-label="`选择${row.name}`"
                @change="toggle(String(row.id))"
              />{{ row.is_dir ? '📁' : '▤' }} {{ row.name }}</label
            ><span>{{ row.is_dir ? '目录' : `${row.size ?? '—'} 字节` }}</span
            ><button
              v-if="row.is_dir"
              class="text-link"
              @click="browse(String(row.id), String(row.name))"
            >
              进入目录 →</button
            ><button
              v-if="shareOnly && !row.is_dir"
              class="text-link"
              @click="fixing = { id: String(row.id), name: String(row.name) }"
            >
              修正识别
            </button>
          </article>
        </div>
        <button
          v-if="sharePage.has_more"
          class="button"
          :disabled="preview.loading.value"
          @click="browse(cid, '', true)"
        >
          读取下一页
        </button>
        <div v-if="!shareOnly" class="form-grid">
          <DirectoryPicker
            v-model="target"
            :name="targetName"
            :account-id="cloud.id"
            label="转存目标目录"
            @selected="(v) => (targetName = v.name)"
          /><label class="checkbox-label"
            ><input v-model="organize" type="checkbox" />转存后整理</label
          ><DirectoryPicker
            v-if="organize"
            v-model="archive"
            :account-id="cloud.id"
            label="整理归档目录"
          /><label v-if="organize" class="checkbox-label"
            ><input v-model="generate" type="checkbox" />整理后生成 STRM</label
          >
        </div>
        <button
          class="button primary"
          :disabled="
            !selected.size ||
            selected.size > 100 ||
            (!shareOnly && !target) ||
            (organize && !shareOnly && !archive) ||
            preview.loading.value ||
            op.busy.value ||
            op.uncertain.value
          "
          @click="planShare"
        >
          {{ shareOnly ? '识别并预览分享 STRM' : '核对转存范围' }}
        </button></template
      >
    </section>
    <section v-if="!shareOnly && tab === 'offline'" class="panel">
      <h2>115 离线任务</h2>
      <p v-if="offline.loading.value" class="muted-text">正在读取离线任务…</p>
      <p
        v-else-if="!offline.error.value && !records(offline.data.value?.items).length"
        class="empty-message"
      >
        当前没有离线任务。
      </p>
      <p v-if="offline.error.value" class="inline-error">{{ offline.error.value }}</p>
      <EvidenceDetails :value="offline.data.value?.items" />
      <div class="pagination">
        <button
          class="button"
          :disabled="offlinePage <= 1 || offline.loading.value"
          @click="
            offlinePage--;
            loadOffline();
          "
        >
          上一页</button
        ><span>第 {{ offlinePage }} 页</span
        ><button
          class="button"
          :disabled="
            offline.loading.value ||
            (!!offline.data.value?.page_count &&
              offlinePage >= Number(offline.data.value.page_count))
          "
          @click="
            offlinePage++;
            loadOffline();
          "
        >
          下一页</button
        ><button class="button" @click="loadOffline">刷新</button>
      </div>
    </section>
    <section v-if="!shareOnly && tab === 'bot'" class="panel">
      <h2>机器人资源接收</h2>
      <label class="checkbox-label"
        ><input v-model="receivingForm.tg_enabled" type="checkbox" />允许白名单用户通过 Telegram Bot
        提交资源</label
      ><label class="checkbox-label"
        ><input
          v-model="receivingForm.organize_after_transfer"
          type="checkbox"
        />接收后自动整理</label
      >
      <p class="muted-text">先在系统设置保存 Bot 与用户白名单，再配置接收和归档目录。</p>
      <p v-if="receiving.error.value" class="inline-error">{{ receiving.error.value }}</p>
      <div class="action-row">
        <button class="button" @click="loadReceiving">读取已保存配置</button
        ><button
          class="button primary"
          :disabled="
            !receiving.data.value || !!receiving.error.value || op.busy.value || op.uncertain.value
          "
          @click="saveReceiving"
        >
          保存接收配置
        </button>
      </div>
    </section>
    <section v-if="plan" class="panel preview-panel">
      <h2>操作预览</h2>
      <EvidenceDetails :value="plan" /><button
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirm = true"
      >
        核对并提交
      </button>
    </section></template
  ><TaskReceipt v-if="job" :id="job" /><ConfirmAction
    v-if="confirm && plan"
    :title="shareOnly ? '生成分享 STRM' : '提交资源接收'"
    :description="
      shareOnly
        ? '仅从当前选择生成分享库 STRM，不转存到个人网盘。STRM 生成后还需单独核实 Emby 实际入库。'
        : `把已预览内容接收到当前账号所选目录${organize ? '，并执行整理归档' : ''}；后台任务接受不代表处理或入库完成。`
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirm = false"
    @confirm="execute"
    ><EvidenceDetails :value="plan" /></ConfirmAction
  ><ConfirmAction
    v-if="directConfirm"
    title="提交资源接收"
    :description="`将输入的资源接收到当前账号已保存目录${organize ? '，并按已保存配置整理' : ''}。磁力和 ed2k 需要等待 115 下载完成。`"
    :busy="op.busy.value"
    :disabled="op.uncertain.value || !directoryConfig.data.value || !!directoryConfig.error.value"
    @close="directConfirm = false"
    @confirm="submitDirect"
  >
    <div class="destination-strip">
      <span v-for="destination in destinations" :key="destination.role"
        ><small>{{ destination.name }}</small
        ><strong>{{ destination.directory }}</strong></span
      >
    </div> </ConfirmAction
  ><MediaOverride
    v-if="fixing"
    :file-id="fixing.id"
    :name="fixing.name"
    @close="fixing = undefined"
    @save="
      (v) => {
        overrides[v.id] = v.choice;
        fixing = undefined;
        invalidate();
      }
    "
  />
</template>
