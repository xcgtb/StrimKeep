<script setup lang="ts">
import { computed, ref, watch } from 'vue';
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
import DirectoryRoles from '../components/DirectoryRoles.vue';
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
    hint: 'self = 内置接收；@机器人用户名 = 使用已登录的用户会话发送。',
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
  if (['command_prefix', 'forward_target'].includes(f.key))
    return draft.value?.action === 'forward';
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
  const allowed = ['id', 'revision', ...fields.map((f) => f.key), 'rule_cards'];
  draft.value = row
    ? Object.fromEntries(allowed.filter((k) => k in row).map((k) => [k, structuredClone(row[k])]))
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
    draft.value = undefined;
    await load();
  }
}
async function previewPublic() {
  if (!draft.value || !cloud.id || draft.value.mode !== 'public_web') return;
  await publicPreview.run(() =>
    writeData(accountPath(cloud.id, 'channels/public-preview'), {
      address: String(draft.value?.address || ''),
      limit: Number(draft.value?.history_limit ?? 20),
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
    title="频道与机器人"
    subtitle="管理 Telegram 频道筛选、接收目录与用户会话。"
  /><AccountPicker /><DirectoryRoles :roles="['channel']" title="频道默认接收目录" />
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
    :receipt="op.receipt.value"
  /><template v-if="cloud.id"
    ><template v-if="tab === 'channels' || tab === 'forward'"
      ><p v-if="tab === 'forward'" class="notice-bar">
        监控频道 → 按筛选规则匹配 → 附加命令头。内置 /strm 直接生成分享
        STRM；转发给其他机器人需登录用户会话。回执只确认发送，不代表对方已入库。
      </p>
      <button class="button primary" @click="edit()">
        {{ tab === 'forward' ? '添加转发规则' : '添加频道' }}
      </button>
      <div class="module-grid">
        <article v-for="r in visibleRules" :key="String(r.id)" class="panel module-card">
          <h2>{{ r.name }}</h2>
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
            >{{ r.enabled ? '已启用' : '已暂停' }} ·
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
              Number(object(r.public_status).next_poll)
                ? new Date(Number(object(r.public_status).next_poll) * 1000).toLocaleString()
                : '等待调度'
            }}
            · 游标 #{{ object(r.public_status).cursor ?? 0 }}
          </p>
          <div class="action-row">
            <button class="button small" @click="edit(r)">编辑规则</button
            ><button class="button small" @click="testId = String(r.id)">测试筛选</button
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
      <section v-if="draft" class="panel">
        <header class="section-heading">
          <h2>编辑频道规则</h2>
          <button class="text-link" @click="draft = undefined">取消编辑</button>
        </header>
        <p class="muted-text">
          填写频道名称、地址、读取模式与处理动作。转存需选择保存目录；生成分享 STRM
          无需转存目录。首次历史条数仅在建立游标时生效，重新启用会继续上次游标；修改筛选规则不回放旧消息。
        </p>
        <FormFields
          v-model="draft"
          :fields="fields.filter(visibleField)"
          :disabled="op.busy.value || op.uncertain.value"
        /><DirectoryPicker
          v-if="transferAction"
          v-model="target"
          :account-id="cloud.id"
          label="频道保存目录"
        />
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
              :aria-pressed="strings(genre.ids).every((id) => strings(draft?.genres).includes(id))"
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
        <div class="action-row">
          <button
            class="button primary"
            :disabled="(transferAction && !target) || op.busy.value || op.uncertain.value"
            @click="save"
          >
            保存频道规则
          </button>
        </div>
      </section>
      <section v-if="testId" class="panel">
        <h2>测试已保存频道规则</h2>
        <label>示例消息<textarea v-model="message" maxlength="8000" rows="5"></textarea></label
        ><button
          class="button"
          :disabled="!message || op.busy.value || op.uncertain.value"
          @click="
            op.run(() =>
              writeData(accountPath(cloud.id, `channels/${testId}/test`), { text: message }),
            )
          "
        >
          测试匹配与原因
        </button>
      </section></template
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
      <FormFields
        v-model="readerForm"
        :fields="readerFields"
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
        ><button
          class="button"
          :disabled="op.busy.value || op.uncertain.value"
          @click="readerAction('import')"
        >
          导入用户会话</button
        ><button class="button danger-text" @click="confirmation = 'reader-disconnect'">
          断开用户会话
        </button>
      </div>
      <p class="muted-text">
        敏感登录信息不会写入浏览器本地存储。服务器使用加密会话读取所配置频道。
      </p>
    </section>
    <section v-if="tab === 'history'" class="panel">
      <h2>接收历史</h2>
      <article
        v-for="entry in records(inbox.data.value?.items)"
        :key="String(entry.id)"
        class="panel"
      >
        <strong
          >{{ entry.source === 'channel' ? '频道' : '资源接收' }} ·
          {{ object(entry.result).channel || entry.id }}</strong
        >
        <p>{{ object(entry.job).message || entry.state }}</p>
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
        <EvidenceDetails :value="entry" />
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
