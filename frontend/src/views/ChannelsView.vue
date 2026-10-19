<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { date, stateLabel } from '../composables/format';
import ModalDialog from '../components/ModalDialog.vue';
import LoadState from '../components/LoadState.vue';
import { useCloud } from '../stores/cloud';
import {
  accountPath,
  readData,
  writeData,
  removeData,
  records,
  object,
  strings,
  type Data,
} from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import PageHeading from '../components/PageHeading.vue';
import AccountPicker from '../components/AccountPicker.vue';
import DirectoryPicker from '../components/DirectoryPicker.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import MediaOverride from '../components/MediaOverride.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  rules = useRequest<Data>(),
  reader = useRequest<Data>(),
  catalog = useRequest<Data>(),
  inbox = useRequest<Data>(),
  publicPreview = useRequest<Data>(),
  inspection = useRequest<Data>(),
  matchPreview = useRequest<Data>(),
  inspectId = ref(''),
  feedback = ref(''),
  filterOpen = ref(false),
  tab = ref('channels'),
  draft = ref<Data>(),
  target = ref(''),
  removeId = ref(''),
  testId = ref(''),
  message = ref(''),
  historyOffset = ref(0),
  readerForm = ref<Data>({
    api_id: '',
    api_hash: '',
    phone: '',
    code: '',
    password: '',
    session: '',
  }),
  ruleMovie = ref(false),
  confirmation = ref('');
const fields: Field[] = [
  { key: 'name', label: '频道名称' },
  { key: 'address', label: '频道地址 / @名称 / -100 ID' },
  {
    key: 'mode',
    label: '读取模式',
    type: 'select',
    hint: '公开频道优先选免登录网页抓取；它不是稳定消息 API，网页预览受限时需改用用户会话。',
    options: [
      { value: 'public_web', label: '公开网页抓取（免登录，尽力而为）' },
      { value: 'user', label: '用户会话与历史读取' },
      { value: 'bot', label: '机器人实时接收（需 Bot 是频道管理员）' },
    ],
  },
  {
    key: 'action',
    label: '处理动作',
    type: 'select',
    options: [
      { value: 'transfer', label: '接收转存到 115' },
      { value: 'share_strm', label: '生成分享 STRM（免转存）' },
      { value: 'forward', label: '万能转发（带命令头）' },
    ],
  },
  {
    key: 'command_prefix',
    label: '转发命令头',
    hint: '内置接收：/strm 免转存生成，/receive 转存；其他机器人可填写自己的命令。',
  },
  {
    key: 'forward_target',
    label: '目标机器人',
    hint: '填写 @机器人用户名；使用已登录的 Telegram 用户会话发送。',
  },
  { key: 'enabled', label: '启用频道', type: 'check' },
  { key: 'auto_transfer', label: '匹配消息后自动处理（关闭则仅记录）', type: 'check' },
  {
    key: 'history_limit',
    label: '首次历史条数（机器人模式为 0）',
    type: 'number',
    min: 0,
    max: 1000,
  },
  { key: 'poll_seconds', label: '用户/公开网页轮询秒数', type: 'number', min: 30, max: 3600 },
  { key: 'include', label: '包含关键词（每行一个）', type: 'lines' },
  { key: 'exclude', label: '排除关键词', type: 'lines' },
  { key: 'regex', label: '正则匹配', type: 'lines' },
  { key: 'tmdb_ids', label: '指定作品（movie:编号 / tv:编号，每行一个）', type: 'lines' },
  { key: 'kinds', label: '允许类型（movie / tv，每行一个）', type: 'lines' },
  { key: 'countries', label: '允许地区（CN / US 等两位代码）', type: 'lines' },
  { key: 'genres', label: '允许题材（TMDB 数字 ID）', type: 'lines' },
];
const transferAction = computed(
  () =>
    !draft.value?.action ||
    draft.value.action === 'transfer' ||
    (draft.value.action === 'forward' &&
      draft.value.forward_target === 'self' &&
      String(draft.value.command_prefix).split(/\s|@/)[0] === '/receive'),
);
const visibleRules = computed(() =>
  records(rules.data.value?.items).filter((r) =>
    tab.value === 'forward' ? r.action === 'forward' : r.action !== 'forward',
  ),
);
function visibleField(f: Field) {
  if (['countries', 'genres'].includes(f.key)) return false;
  if (f.key === 'command_prefix') return draft.value?.action === 'forward';
  if (f.key === 'forward_target' && draft.value?.action !== 'forward') return false;
  if (f.key === 'forward_target') return draft.value?.forward_target !== 'self';
  if (['history_limit', 'poll_seconds'].includes(f.key)) return draft.value?.mode !== 'bot';
  if (f.key === 'organize') return transferAction.value;
  return true;
}
watch(
  () => draft.value?.mode,
  (mode) => {
    if (!draft.value) return;
    if (mode === 'bot') draft.value.history_limit = 0;
    if (mode === 'public_web') {
      draft.value.history_limit = Math.min(Number(draft.value.history_limit) || 0, 100);
      draft.value.poll_seconds = Math.max(Number(draft.value.poll_seconds) || 300, 300);
    }
  },
);
const groupFields = (keys: string[]) =>
  fields
    .filter((f) => keys.includes(f.key) && visibleField(f))
    .map((f) => {
      if (f.key === 'history_limit' && draft.value?.mode === 'public_web')
        return { ...f, max: 100 };
      if (f.key === 'poll_seconds' && draft.value?.mode === 'public_web') return { ...f, min: 300 };
      return f;
    });
const modeHint = computed(() =>
  draft.value?.mode === 'user'
    ? '使用你登录的 Telegram 账号读取有权限的频道，支持历史消息。'
    : draft.value?.mode === 'bot'
      ? '只接收 Bot 加入并担任管理员的频道新消息；不能读取他人的频道历史。'
      : '直接读取 Telegram 公开预览，不需要 API ID 或登录；私人频道和未开放预览的频道不可用。',
);
const routeHint = computed(() =>
  !draft.value
    ? ''
    : draft.value.action === 'forward' && draft.value.forward_target !== 'self'
      ? `匹配消息 → 添加 ${draft.value.command_prefix || '命令头'} → 发给 ${draft.value.forward_target || '目标机器人'}。需要先登录用户会话。`
      : transferAction.value
        ? '匹配资源 → 转存到接收目录。后续整理与生成 STRM 由影视整理规则执行。'
        : '匹配 115 分享 → 内置识别与选版 → 生成分享 STRM，无需先转存到你的网盘。',
);
function actionLabel(rule: Data) {
  return rule.action === 'forward'
    ? rule.forward_target === 'self'
      ? '命令交给内置处理'
      : `发送给 ${rule.forward_target}`
    : rule.action === 'share_strm'
      ? '免转存生成分享 STRM'
      : '转存到 115';
}
function rulePayload(row: Data): Data {
  const allowed = ['id', 'revision', ...fields.map((f) => f.key), 'target_cid', 'rule_cards'];
  return Object.fromEntries(
    allowed.filter((key) => key in row).map((key) => [key, structuredClone(row[key])]),
  );
}
async function toggleRule(row: Data) {
  const result = await op.run(() =>
    writeData(accountPath(cloud.id, `channels/${row.id}/enabled`), {
      revision: row.revision,
      enabled: !row.enabled,
    }),
  );
  if (result) {
    feedback.value = row.enabled
      ? '频道已暂停；尚未开始的处理会停止。已生成的后续任务请到任务中心查看。'
      : '频道已启用；轮询模式等待调度，Bot 模式等待新消息。';
    await rules.run((signal) => readData(accountPath(cloud.id, 'channels'), signal));
  }
}
async function inspectRule(row: Data) {
  inspection.cancel();
  inspection.data.value = undefined;
  inspectId.value = String(row.id);
  await inspection.run(() => writeData(accountPath(cloud.id, `channels/${row.id}/inspect`)));
}
function testRule(row: Data) {
  testId.value = String(row.id);
  message.value = '';
  matchPreview.data.value = undefined;
}
async function testMessage() {
  await matchPreview.run(() =>
    writeData(accountPath(cloud.id, `channels/${testId.value}/test`), { text: message.value }),
  );
}
useVisiblePoll(async () => {
  if (!cloud.id || op.busy.value) return;
  if (tab.value === 'history') await loadInbox();
  else await rules.run((signal) => readData(accountPath(cloud.id, 'channels'), signal));
}, 10000);
const readerFields: Field[] = [
  { key: 'api_id', label: 'Telegram API ID' },
  { key: 'api_hash', label: 'API Hash', type: 'secret' },
  { key: 'phone', label: '手机号（包含国际区号）' },
  { key: 'session', label: 'StringSession（仅会话导入使用）', type: 'secret' },
  { key: 'code', label: '登录验证码' },
  { key: 'password', label: '两步验证密码', type: 'secret' },
];
const cardFields: Field[] = [
  { key: 'id', label: '作品编号（movie: / tv:）' },
  { key: 'label', label: '显示片名' },
  { key: 'year', label: '年份' },
  { key: 'poster_path', label: '海报文件路径（可留空）' },
  {
    key: 'resolution',
    label: '分辨率',
    type: 'select',
    options: [
      { value: '', label: '不限' },
      { value: '2160p', label: '2160p' },
      { value: '1080p', label: '1080p' },
      { value: '720p', label: '720p' },
    ],
  },
  {
    key: 'dynamic_range',
    label: '动态范围',
    type: 'select',
    options: [
      { value: '', label: '不限' },
      { value: 'DV', label: '杜比视界' },
      { value: 'HDR', label: 'HDR' },
      { value: 'SDR', label: 'SDR' },
    ],
  },
  { key: 'keywords', label: '作品关键词' },
];
async function load() {
  if (!cloud.id) return;
  const aid = cloud.id;
  await Promise.allSettled([
    rules.run((signal) => readData(accountPath(aid, 'channels'), signal)),
    reader.run((signal) => readData(accountPath(aid, 'telegram-reader'), signal)),
    loadInbox(),
    catalog.run((signal) => readData('/api/cloud/resources/catalog', signal)),
  ]);
}
function loadInbox() {
  return inbox.run((signal) =>
    readData(accountPath(cloud.id, 'inbox') + `?offset=${historyOffset.value}`, signal),
  );
}
function edit(row?: Data) {
  filterOpen.value = false;
  draft.value = row
    ? rulePayload(row)
    : {
        name: '',
        address: '',
        action: tab.value === 'forward' ? 'forward' : 'transfer',
        command_prefix: '/strm',
        forward_target: 'self',
        mode: 'public_web',
        auto_transfer: true,
        enabled: false,
        organize: false,
        history_limit: 20,
        poll_seconds: 300,
        include: [],
        exclude: [],
        regex: [],
        tmdb_ids: [],
        kinds: [],
        countries: [],
        genres: [],
        rule_cards: [],
      };
  draft.value.action ||= 'transfer';
  draft.value.command_prefix ||= '/strm';
  draft.value.forward_target ||= 'self';
  target.value = String(row?.target_cid || '');
  publicPreview.data.value = undefined;
}
async function save() {
  if (!draft.value) return;
  const r = await op.run(() =>
    writeData(accountPath(cloud.id, 'channels'), { ...draft.value, target_cid: target.value }),
  );
  if (r) {
    feedback.value = draft.value.enabled
      ? '规则已保存并启用。'
      : '规则已保存为暂停状态。先读取并检查，再点启用。';
    tab.value = draft.value.action === 'forward' ? 'forward' : 'channels';
    draft.value = undefined;
    await load();
  }
}
async function previewPublic() {
  if (!draft.value || !cloud.id || draft.value.mode !== 'public_web') return;
  await publicPreview.run(() =>
    writeData(accountPath(cloud.id, 'channels/public-preview'), {
      address: String(draft.value?.address || ''),
      limit: Math.max(1, Number(draft.value?.history_limit ?? 20)),
    }),
  );
}
async function readerAction(action: string) {
  const body: Data = { action, expected_revision: reader.data.value?.revision };
  if (action === 'code' || action === 'import') {
    body.api_id = readerForm.value.api_id;
    body.api_hash = readerForm.value.api_hash;
    if (action === 'code') body.phone = readerForm.value.phone;
    else body.session = readerForm.value.session;
  }
  if (action === 'verify') {
    body.code = readerForm.value.code;
    body.password = readerForm.value.password;
  }
  const r = await op.run(() => writeData(accountPath(cloud.id, 'telegram-reader'), body));
  for (const k of ['api_hash', 'password', 'code', 'session']) readerForm.value[k] = '';
  if (r) reader.data.value = r;
}
async function execute() {
  if (confirmation.value === 'reader-disconnect') {
    await readerAction('disconnect');
  } else if (confirmation.value === 'delete') {
    const r = await op.run(() => removeData(accountPath(cloud.id, `channels/${removeId.value}`)));
    if (r) {
      removeId.value = '';
      await load();
    }
  }
  confirmation.value = '';
}
function updateCard(i: number, v: Data) {
  if (draft.value)
    draft.value = {
      ...draft.value,
      rule_cards: records(draft.value.rule_cards).map((c, j) => (j === i ? v : c)),
    };
}
function toggleCatalog(key: 'countries' | 'genres', ids: string[]) {
  if (!draft.value || op.busy.value || op.uncertain.value) return;
  const selected = new Set(strings(draft.value[key]));
  const remove = ids.every((id) => selected.has(id));
  ids.forEach((id) => (remove ? selected.delete(id) : selected.add(id)));
  draft.value = { ...draft.value, [key]: [...selected] };
}
watch(
  () => cloud.id,
  () => {
    rules.cancel();
    reader.cancel();
    inbox.cancel();
    publicPreview.cancel();
    inspection.cancel();
    matchPreview.cancel();
    inspectId.value = '';
    testId.value = '';
    feedback.value = '';
    publicPreview.data.value = undefined;
    op.reconcile();
    draft.value = undefined;
    target.value = '';
    confirmation.value = '';
    historyOffset.value = 0;
    readerForm.value = { api_id: '', api_hash: '', phone: '', code: '', password: '', session: '' };
    void load();
  },
  { immediate: true },
);
</script>
<template>
  <PageHeading
    title="115 频道监控"
    subtitle="从频道发现资源，按规则转存、生成分享 STRM 或转交机器人。"
  /><AccountPicker />
  <nav class="browse-tabs">
    <button
      v-for="(label, key) in {
        channels: '频道规则',
        forward: '万能转发',
        reader: '用户会话',
        history: '接收历史',
      }"
      :key="key"
      :class="{ active: tab === key }"
      @click="tab = key"
    >
      {{ label }}</button
    ><button
      class="text-link push-right"
      @click="
        load();
        op.reconcile();
      "
    >
      刷新并核实
    </button>
  </nav>
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value || rules.error.value || reader.error.value || inbox.error.value"
    :uncertain="op.uncertain.value"
  />
  <p v-if="feedback" class="notice-bar" role="status">{{ feedback }}</p>
  <template v-if="cloud.id"
    ><template v-if="tab === 'channels' || tab === 'forward'"
      ><section class="panel channel-guide">
        <div>
          <span class="eyebrow">{{ tab === 'forward' ? '万能转发' : '自动接收' }}</span>
          <h2>
            {{ tab === 'forward' ? '先选择接收者，再设置命令' : '一个频道，一条清楚的处理规则' }}
          </h2>
          <p>
            {{
              tab === 'forward'
                ? '内置处理直接执行 /strm 或 /receive；发给其他机器人需要 Telegram 用户会话。'
                : '公开频道可直接填地址；保存后先检查读取结果，再启用监控。'
            }}
          </p>
        </div>
        <ol class="channel-steps">
          <li><strong>1. 配置来源</strong><span>地址与读取方式</span></li>
          <li><strong>2. 选择处理</strong><span>筛选、接收者与动作</span></li>
          <li><strong>3. 检查并启用</strong><span>查看样本和接收历史</span></li>
        </ol>
      </section>
      <p v-if="tab === 'forward'" class="notice-bar">
        监控频道 → 按筛选规则匹配 → 附加命令头。内置 /strm 直接生成分享
        STRM；转发给其他机器人需登录用户会话。回执只确认发送，不代表对方已入库。
      </p>
      <button class="button primary" @click="edit()">
        {{ tab === 'forward' ? '添加转发规则' : '添加频道' }}
      </button>
      <LoadState
        :loading="rules.loading.value && !rules.data.value"
        :error="rules.error.value"
        :empty="!!rules.data.value && !visibleRules.length"
        :message="
          tab === 'forward'
            ? '还没有转发规则，添加一个频道和接收者开始。'
            : '还没有频道规则，添加公开频道后可先做只读检查。'
        "
        @retry="load"
      />
      <div class="module-grid channel-rule-grid">
        <article v-for="r in visibleRules" :key="String(r.id)" class="panel module-card">
          <header class="section-heading">
            <h2>{{ r.name }}</h2>
            <span class="small-status" :class="{ 'channel-enabled': r.enabled }">{{
              r.enabled ? '已启用' : '已暂停'
            }}</span>
          </header>
          <p>
            {{ r.address }} ·
            {{
              r.action === 'share_strm'
                ? '分享 STRM（免转存）'
                : r.action === 'forward'
                  ? '万能转发 → ' +
                    (r.forward_target === 'self' ? '内置接收' : r.forward_target) +
                    ' · ' +
                    r.command_prefix
                  : '接收转存'
            }}
          </p>
          <span class="small-status"
            >{{ r.auto_transfer === false ? '仅记录匹配消息' : '匹配后自动处理' }} ·
            {{
              r.mode === 'public_web' ? '公开网页' : r.mode === 'user' ? '用户会话' : '机器人'
            }}</span
          >
          <p v-if="r.reader_status" class="inline-error">{{ r.reader_status }}</p>
          <p v-if="r.mode !== 'bot'" class="muted-text">
            上次成功：{{
              Number(object(r.public_status).last_success)
                ? new Date(Number(object(r.public_status).last_success) * 1000).toLocaleString()
                : '尚未成功'
            }}
            · 最近新消息：{{ object(r.public_status).last_count ?? 0 }} · 失败次数：{{
              object(r.public_status).failures ?? 0
            }}
          </p>
          <p v-if="r.mode !== 'bot'" class="muted-text">
            下次检查：{{
              !r.enabled
                ? '已暂停，不再轮询'
                : Number(object(r.public_status).next_poll)
                  ? new Date(Number(object(r.public_status).next_poll) * 1000).toLocaleString()
                  : '等待调度'
            }}
          </p>
          <p class="muted-text">
            {{ actionLabel(r)
            }}<template v-if="r.action === 'transfer'"> · 接收目录 {{ r.target_cid }}</template>
          </p>
          <div class="action-row">
            <button
              class="button small"
              :class="{ primary: !r.enabled }"
              :disabled="op.busy.value || op.uncertain.value"
              @click="toggleRule(r)"
            >
              {{ r.enabled ? '暂停监控' : '启用监控' }}
            </button>
            <button class="button small" @click="inspectRule(r)">读取并检查</button>
            <button class="button small" @click="edit(r)">编辑规则</button
            ><button class="button small" @click="testRule(r)">测试筛选</button
            ><button
              class="button small danger-text"
              @click="
                removeId = String(r.id);
                confirmation = 'delete';
              "
            >
              移除
            </button>
          </div>
        </article>
      </div>
      <ModalDialog v-if="draft" title="编辑频道规则" @close="!op.busy.value && (draft = undefined)">
        <OperationState
          :busy="op.busy.value"
          :error="op.error.value"
          :uncertain="op.uncertain.value"
        />
        <section class="channel-form-section">
          <h3>1. 频道来源</h3>
          <FormFields
            v-model="draft"
            :fields="groupFields(['name', 'address', 'mode'])"
            :disabled="op.busy.value || op.uncertain.value"
          />
          <p class="notice-bar">{{ modeHint }}</p>
          <p v-if="draft.mode === 'user' && !reader.data.value?.connected" class="inline-error">
            尚未登录用户会话。可先保存暂停规则，关闭编辑后到「用户会话」登录。
          </p>
        </section>
        <section class="channel-form-section">
          <h3>2. 匹配后做什么</h3>
          <FormFields
            v-model="draft"
            :fields="groupFields(['action'])"
            :disabled="op.busy.value || op.uncertain.value"
          />
          <label v-if="draft.action === 'forward'" class="channel-target"
            >接收位置<select
              aria-label="接收位置"
              :value="draft.forward_target === 'self' ? 'self' : 'external'"
              :disabled="op.busy.value || op.uncertain.value"
              @change="
                draft.forward_target =
                  ($event.target as HTMLSelectElement).value === 'self' ? 'self' : ''
              "
            >
              <option value="self">StrimKeep 内置处理</option>
              <option value="external">其他 Telegram 机器人</option>
            </select></label
          >
          <FormFields
            v-model="draft"
            :fields="groupFields(['command_prefix', 'forward_target'])"
            :disabled="op.busy.value || op.uncertain.value"
          />
          <p class="notice-bar">{{ routeHint }}</p>
          <DirectoryPicker
            v-if="transferAction"
            v-model="target"
            :account-id="cloud.id"
            label="频道保存目录"
            :disabled="op.busy.value || op.uncertain.value"
          />
        </section>
        <section class="channel-form-section">
          <h3>3. 运行方式</h3>
          <FormFields
            v-model="draft"
            :fields="groupFields(['enabled', 'auto_transfer', 'history_limit', 'poll_seconds'])"
            :disabled="op.busy.value || op.uncertain.value"
          />
          <p class="muted-text">
            首次历史仅在建立读取位置时生效；暂停后再启用继续上次位置。修改地址或读取方式会重建位置。
          </p>
          <p v-if="draft.auto_transfer === false" class="notice-bar">
            观察模式：只记录匹配消息，不转存、不生成 STRM、不向其他机器人发送。
          </p>
        </section>
        <section v-if="draft.mode === 'public_web'" class="panel">
          <h3>公开网页读取测试（只读）</h3>
          <p class="muted-text">
            仅读取 Telegram 公开预览页，不保存游标、不登记任务、不触发 115 转存。需要可以公开访问的
            t.me 频道用户名。
          </p>
          <button
            class="button"
            :disabled="publicPreview.loading.value || op.busy.value"
            @click="previewPublic"
          >
            测试公开网页抓取
          </button>
          <OperationState :busy="publicPreview.loading.value" :error="publicPreview.error.value" />
          <template v-if="publicPreview.data.value">
            <p>成功读取 {{ publicPreview.data.value.count }} 条公开消息（显示最多 20 个链接）。</p>
            <div
              v-for="item in records(publicPreview.data.value.messages)"
              :key="String(item.message_id)"
              class="panel"
            >
              <strong>消息 #{{ item.message_id }}</strong>
              <p class="muted-text">{{ String(item.text || '').slice(0, 200) }}</p>
              <p v-for="link in strings(item.links)" :key="String(link)" class="path-text">
                {{ link }}
              </p>
            </div>
          </template>
        </section>
        <details
          :open="filterOpen"
          class="channel-advanced"
          @toggle="filterOpen = ($event.target as HTMLDetailsElement).open"
        >
          <summary>筛选规则与指定作品（可选）</summary>
          <p class="muted-text">
            不设置表示接收所有符合处理动作的资源；包含关键词任意命中一个，排除关键词命中即跳过。
          </p>
          <FormFields
            v-model="draft"
            :fields="groupFields(['include', 'exclude', 'regex', 'tmdb_ids', 'kinds'])"
            :disabled="op.busy.value || op.uncertain.value"
          />
          <section class="catalog-filters">
            <h3 class="detail-section-title">地区与题材筛选</h3>
            <p class="muted-text">留空表示不限；题材会同时选择后端提供的电影与剧集分类。</p>
            <p v-if="catalog.error.value" class="inline-error">{{ catalog.error.value }}</p>
            <h4>允许地区</h4>
            <div class="source-checkboxes">
              <button
                v-for="country in records(catalog.data.value?.countries)"
                :key="String(country.id)"
                class="button small"
                :class="{ active: strings(draft.countries).includes(String(country.id)) }"
                :aria-pressed="strings(draft.countries).includes(String(country.id))"
                @click="toggleCatalog('countries', [String(country.id)])"
              >
                {{ country.name }}
              </button>
            </div>
            <h4>允许题材</h4>
            <div class="source-checkboxes">
              <button
                v-for="genre in records(catalog.data.value?.genres)"
                :key="String(genre.name)"
                class="button small"
                :class="{
                  active: strings(genre.ids).every((id) => strings(draft?.genres).includes(id)),
                }"
                :aria-pressed="
                  strings(genre.ids).every((id) => strings(draft?.genres).includes(id))
                "
                @click="toggleCatalog('genres', strings(genre.ids))"
              >
                {{ genre.name }}
              </button>
            </div>
            <details>
              <summary>直接编辑地区代码与题材编号</summary>
              <FormFields
                v-model="draft"
                :fields="fields.filter((f) => ['countries', 'genres'].includes(f.key))"
                :disabled="op.busy.value || op.uncertain.value"
              />
            </details>
          </section>
          <h3 class="detail-section-title">作品规则卡片</h3>
          <div v-for="(card, i) in records(draft.rule_cards)" :key="i" class="panel repeat-card">
            <FormFields
              :model-value="card"
              :fields="cardFields"
              @update:model-value="(v) => updateCard(i, v)"
            /><button
              class="text-link danger-text"
              @click="draft.rule_cards = records(draft.rule_cards).filter((_, j) => j !== i)"
            >
              移除此作品规则
            </button>
          </div>
          <button class="button small" @click="ruleMovie = true">搜索并添加作品规则</button>
        </details>
        <template #footer
          ><button class="button" :disabled="op.busy.value" @click="draft = undefined">取消</button>
          <button
            class="button primary"
            :disabled="(transferAction && !target) || op.busy.value || op.uncertain.value"
            @click="save"
          >
            保存频道规则
          </button>
        </template>
      </ModalDialog>
      <ModalDialog
        v-if="testId"
        title="测试已保存频道规则"
        @close="
          testId = '';
          matchPreview.cancel();
        "
      >
        <p class="muted-text">粘贴一条消息，检查筛选和资源类型。仅预览，不发送、不转存。</p>
        <label>示例消息<textarea v-model="message" maxlength="8000" rows="5"></textarea></label>
        <button
          class="button primary"
          :disabled="!message.trim() || matchPreview.loading.value"
          @click="testMessage"
        >
          测试匹配与原因
        </button>
        <OperationState :busy="matchPreview.loading.value" :error="matchPreview.error.value" />
        <section v-if="matchPreview.data.value" class="channel-check-result" aria-live="polite">
          <h3>{{ matchPreview.data.value.matched ? '消息符合规则' : '消息不会处理' }}</h3>
          <p>{{ matchPreview.data.value.reason }}</p>
          <p>{{ matchPreview.data.value.would_do }}</p>
          <pre v-if="matchPreview.data.value.preview_text" class="channel-command">{{
            matchPreview.data.value.preview_text
          }}</pre>
        </section>
      </ModalDialog>
      <ModalDialog
        v-if="inspectId"
        title="读取与处理检查"
        @close="
          inspectId = '';
          inspection.cancel();
        "
      >
        <OperationState :busy="inspection.loading.value" :error="inspection.error.value" />
        <template v-if="inspection.data.value"
          ><p class="notice-bar">{{ inspection.data.value.note }}</p>
          <ul class="channel-checks">
            <li v-for="check in records(inspection.data.value.checks)" :key="String(check.label)">
              <strong
                >{{ check.ok === true ? '✓' : check.ok === false ? '!' : '—' }}
                {{ check.label }}</strong
              ><span>{{ check.detail }}</span>
            </li>
          </ul>
          <h3>最近消息样本</h3>
          <p v-if="!records(inspection.data.value.messages).length" class="muted-text">
            没有可展示的消息；请查看上面的读取说明，也可以使用「测试筛选」粘贴消息。
          </p>
          <article
            v-for="sample in records(inspection.data.value.messages)"
            :key="String(sample.message_id)"
            class="channel-sample"
          >
            <header>
              <strong>消息 #{{ sample.message_id }}</strong
              ><span class="small-status">{{ sample.matched ? '符合规则' : '跳过' }}</span>
            </header>
            <p class="channel-message">{{ sample.text || '链接位于消息按钮或隐藏文字中' }}</p>
            <p>{{ sample.reason }}</p>
            <p class="muted-text">{{ sample.would_do }}</p>
            <details v-if="sample.preview_text">
              <summary>查看将要发送的命令文本</summary>
              <pre class="channel-command">{{ sample.preview_text }}</pre>
            </details>
          </article>
        </template>
      </ModalDialog></template
    >
    <section v-if="tab === 'reader'" class="panel">
      <h2>Telegram 用户会话</h2>
      <p class="notice-bar">
        {{
          reader.data.value?.connected
            ? '已连接用户会话'
            : reader.data.value?.pending
              ? '验证码已申请，等待验证'
              : '未连接'
        }}
      </p>
      <p class="muted-text">
        读取有权限的非公开频道，或给其他机器人发送命令时使用。仅监控公开网页、交给内置处理时无需配置。
      </p>
      <h3>1. 登录 Telegram 账号</h3>
      <FormFields
        v-model="readerForm"
        :fields="readerFields.filter((f) => ['api_id', 'api_hash', 'phone'].includes(f.key))"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <h3 v-if="reader.data.value?.pending">2. 输入验证码或两步验证密码</h3>
      <FormFields
        v-if="reader.data.value?.pending"
        v-model="readerForm"
        :fields="readerFields.filter((f) => ['code', 'password'].includes(f.key))"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <div class="action-row">
        <button
          class="button"
          :disabled="op.busy.value || op.uncertain.value"
          @click="readerAction('code')"
        >
          申请验证码</button
        ><button
          class="button"
          :disabled="!reader.data.value?.pending || op.busy.value || op.uncertain.value"
          @click="readerAction('verify')"
        >
          验证登录 / 两步验证</button
        ><button class="button danger-text" @click="confirmation = 'reader-disconnect'">
          断开用户会话
        </button>
      </div>
      <details class="channel-advanced">
        <summary>已有 StringSession？导入会话</summary>
        <FormFields
          v-model="readerForm"
          :fields="readerFields.filter((f) => ['api_id', 'api_hash', 'session'].includes(f.key))"
          :disabled="op.busy.value || op.uncertain.value"
        /><button
          class="button"
          :disabled="op.busy.value || op.uncertain.value"
          @click="readerAction('import')"
        >
          导入用户会话
        </button>
      </details>
      <p class="muted-text">
        敏感登录信息不会写入浏览器本地存储。服务器使用加密会话读取所配置频道。
      </p>
    </section>
    <section v-if="tab === 'history'" class="panel">
      <h2>接收历史</h2>
      <p class="muted-text">
        每 10 秒更新。父任务提交成功后，转存与 STRM 的最终结果请继续查看后续任务。
      </p>
      <LoadState
        :loading="inbox.loading.value && !inbox.data.value"
        :error="inbox.error.value"
        :empty="!!inbox.data.value && !records(inbox.data.value.items).length"
        message="尚无接收记录。启用规则并出现新消息后会在这里显示。"
        @retry="loadInbox"
      />
      <article
        v-for="entry in records(inbox.data.value?.items)"
        :key="String(entry.id)"
        class="panel"
      >
        <strong
          >{{ entry.source === 'channel' ? '频道' : '资源接收' }} ·
          {{ object(entry.result).channel || entry.id }}</strong
        >
        <p>
          {{
            object(entry.job).message || (entry.state === 'queued' ? '等待派发任务' : '等待处理')
          }}
        </p>
        <p class="muted-text">
          {{ date(Number(entry.created))
          }}<template v-if="object(entry.job).state">
            · {{ stateLabel(String(object(entry.job).state)) }}</template
          >
        </p>
        <div class="action-row">
          <RouterLink
            v-if="entry.job_id"
            class="button small"
            :to="{ path: '/tasks', query: { job: String(entry.job_id) } }"
            >查看处理任务</RouterLink
          ><RouterLink
            v-for="jid in strings(object(entry.result).child_jobs)"
            :key="jid"
            class="button small"
            :to="{ path: '/tasks', query: { job: jid } }"
            >查看后续任务</RouterLink
          >
        </div>
        <p
          v-for="(error, index) in records(object(entry.result).errors)"
          :key="index"
          class="inline-error"
        >
          {{ error.stage_label || '处理阶段' }}：{{ error.reason
          }}{{ error.uncertain ? ' · 结果待核实，请勿重复提交' : '' }}
        </p>
        <p
          v-for="(error, index) in records(object(entry.result).child_errors)"
          :key="'child' + index"
          class="inline-error"
        >
          后续任务 · {{ error.stage_label || '处理阶段' }}：{{ error.reason }}
        </p>
        <details>
          <summary>展开处理明细</summary>
          <EvidenceDetails :value="entry" />
        </details>
      </article>
      <div class="pagination">
        <button
          class="button"
          :disabled="historyOffset === 0 || inbox.loading.value"
          @click="
            historyOffset = Math.max(0, historyOffset - 50);
            loadInbox();
          "
        >
          上一页</button
        ><button
          class="button"
          :disabled="records(inbox.data.value?.items).length < 50 || inbox.loading.value"
          @click="
            historyOffset += 50;
            loadInbox();
          "
        >
          下一页
        </button>
      </div>
    </section></template
  ><ConfirmAction
    v-if="confirmation"
    title="确认移除配置"
    :description="
      confirmation === 'delete'
        ? '删除当前频道规则，之后停止监控该频道。'
        : '断开当前账号的 Telegram 用户会话，依赖用户会话的频道读取将停止。'
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = ''"
    @confirm="execute"
  /><MediaOverride
    v-if="ruleMovie && draft"
    file-id="channel-rule"
    @close="ruleMovie = false"
    @save="
      (v) => {
        const id = `${v.choice.kind}:${v.choice.tmdb_id}`;
        draft!.tmdb_ids = [...new Set([...strings(draft!.tmdb_ids), id])];
        draft!.rule_cards = [
          ...records(draft!.rule_cards),
          {
            id,
            label: id,
            year: '',
            poster_path: '',
            resolution: '',
            dynamic_range: '',
            keywords: '',
          },
        ];
        ruleMovie = false;
      }
    "
  />
</template>

<style scoped>
.channel-guide {
  display: grid;
  grid-template-columns: 1fr 1.1fr;
  gap: 24px;
  align-items: center;
  margin-bottom: 20px;
}
.channel-guide h2 {
  margin: 8px 0;
}
.channel-guide p {
  color: var(--muted);
  line-height: 1.7;
}
.channel-steps {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  padding: 0;
  margin: 0;
  list-style: none;
}
.channel-steps li {
  display: grid;
  align-content: start;
  gap: 7px;
  padding: 16px 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface2);
}
.channel-steps span {
  color: var(--muted);
  font-size: 12px;
}
.channel-rule-grid {
  margin-top: 18px;
}
.channel-rule-grid article {
  min-width: 0;
}
.channel-rule-grid h2,
.channel-rule-grid p {
  overflow-wrap: anywhere;
}
.channel-enabled {
  color: var(--green);
  background: var(--green-bg);
}
.channel-form-section {
  padding: 4px 0 20px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--line);
}
.channel-form-section h3 {
  margin: 8px 0 16px;
}
.channel-target {
  display: grid;
  gap: 7px;
  margin: 16px 0;
}
.channel-advanced {
  padding: 16px 0;
}
.channel-advanced summary {
  cursor: pointer;
  color: var(--blue);
  font-weight: 600;
  margin-bottom: 16px;
}
.channel-command {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  padding: 14px;
  background: var(--surface2);
  border: 1px solid var(--line);
  border-radius: 10px;
  font-size: 13px;
  line-height: 1.65;
}
.channel-checks {
  list-style: none;
  padding: 0;
  display: grid;
  gap: 12px;
}
.channel-checks li {
  display: grid;
  gap: 6px;
  padding: 12px 0;
  border-bottom: 1px solid var(--line);
}
.channel-checks span {
  color: var(--muted);
  line-height: 1.6;
}
.channel-sample {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
  margin: 14px 0;
}
.channel-sample header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.channel-message {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.7;
  max-height: 220px;
  overflow: auto;
}
.channel-check-result {
  padding: 16px;
  border-radius: 12px;
  background: var(--surface2);
  margin-top: 16px;
}
@media (max-width: 900px) {
  .channel-guide {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 540px) {
  .channel-steps {
    grid-template-columns: 1fr;
    gap: 8px;
  }
  .channel-steps li {
    padding: 12px;
  }
  .channel-guide {
    gap: 14px;
  }
}
</style>
