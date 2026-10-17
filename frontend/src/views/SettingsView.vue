<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { get, post } from '../api/client';
import { object, records, strings, text } from '../api/domain';

const strategy = ref<Record<string, unknown>>({});
const cover = ref<Record<string, unknown>[]>([]);
const ingest = ref<Record<string, unknown>>({});
const auto = ref<Record<string, unknown>>({});
const config = ref<Record<string, unknown>>({});
const bot = ref<Record<string, unknown>>({});
const exemptText = ref('');
const newGroup = ref('');
const message = ref('');
const error = ref('');
const busy = ref(false);

const decisionOptions = [
  { value: 'quality_first', label: '画质优先' },
  { value: 'keep_local', label: '保留本地' },
  { value: 'keep_share', label: '保留分享' },
];
const specialOptions = [
  { value: 'compare', label: '画质对比' },
  { value: 'ignore', label: '忽略' },
  { value: 'delete', label: '清理' },
];
const matchOptions = [
  { value: 'title_year', label: '剧名+年份（安全）' },
  { value: 'tmdb_first', label: 'TMDB 优先（高召回）' },
];
const protectOptions = [
  { value: 'compare', label: '开启' },
  { value: 'off', label: '关闭' },
];

async function loadAll() {
  error.value = '';
  try {
    const [s, c, i, a, cf, b] = await Promise.all([
      get<Record<string, unknown>>('/api/strategy').catch(() => ({} as Record<string, unknown>)),
      get<Record<string, unknown>>('/api/cover-strategy').catch(() => ({} as Record<string, unknown>)),
      get<Record<string, unknown>>('/api/ingest/settings').catch(() => ({} as Record<string, unknown>)),
      get<Record<string, unknown>>('/api/governance/auto').catch(() => ({} as Record<string, unknown>)),
      get<Record<string, unknown>>('/api/config').catch(() => ({} as Record<string, unknown>)),
      get<Record<string, unknown>>('/api/bot/status').catch(() => ({} as Record<string, unknown>)),
    ]);
    strategy.value = object(s.strategy ?? s);
    cover.value = records(object(c.strategy ?? c).rules);
    ingest.value = object(i.settings ?? i);
    auto.value = object(a);
    config.value = object(cf.config ?? cf.settings ?? cf);
    bot.value = object(b);
    exemptText.value = strings(strategy.value.exempt_keywords).join('\n');
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function saveStrategy() {
  busy.value = true;
  try {
    await post('/api/strategy', {
      decision: strategy.value.decision,
      match_strategy: strategy.value.match_strategy || 'title_year',
      multi_season_protect: strategy.value.multi_season_protect || 'compare',
      tie_keep_local: strategy.value.tie_keep_local === true,
      season_replace_ratio: Number(strategy.value.season_replace_ratio ?? 0.9),
      special_action: strategy.value.special_action || 'compare',
      exempt_keywords: exemptText.value
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
    });
    message.value = '治理策略已保存';
    await loadAll();
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function saveCover() {
  busy.value = true;
  try {
    await post('/api/cover-strategy', { rules: cover.value });
    message.value = '画质对比规则已保存';
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function saveIngest() {
  busy.value = true;
  try {
    await post('/api/ingest/settings', ingest.value);
    message.value = '入库监控已保存';
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function saveAuto() {
  busy.value = true;
  try {
    await post('/api/governance/auto', auto.value);
    message.value = '定时巡检已保存';
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function saveConfig() {
  busy.value = true;
  try {
    const editable = [
      'emby_host',
      'http_proxy_enabled',
      'http_proxy_url',
      'http_proxy_username',
      'http_proxy_password',
      'tgto_enabled',
      'tgto_url',
      'tgto_max_points',
      'emby_local_path',
      'emby_share_path',
      'background_light_url',
      'background_dark_url',
    ];
    const payload: Record<string, unknown> = {};
    for (const key of editable) if (key in config.value) payload[key] = config.value[key];
    await post('/api/config', payload);
    message.value = '服务配置已保存';
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
// 画质维度编辑
function moveTier(rule: Record<string, unknown>, from: number, to: number) {
  const tiers = strings(rule.tiers);
  if (to < 0 || to >= tiers.length) return;
  const [item] = tiers.splice(from, 1);
  tiers.splice(to, 0, item);
  rule.tiers = tiers;
}
function moveGroup(rule: Record<string, unknown>, from: number, to: number) {
  const groups = strings(rule.groups);
  if (to < 0 || to >= groups.length) return;
  const [item] = groups.splice(from, 1);
  groups.splice(to, 0, item);
  rule.groups = groups;
}
function addGroup(rule: Record<string, unknown>) {
  const value = newGroup.value;
  if (value) newGroup.value = '';
  if (!value.trim()) return;
  rule.groups = [...strings(rule.groups), value.trim()];
}
function removeGroup(rule: Record<string, unknown>, index: number) {
  rule.groups = strings(rule.groups).filter((_, i) => i !== index);
}
function isMasked(value: unknown) {
  return typeof value === 'string' && /\*/.test(value);
}
function shown(value: unknown) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? '开' : '关';
  if (typeof value === 'object') return JSON.stringify(value);
  return text(value);
}
onMounted(loadAll);
</script>
<template>
  <section class="tab active">
    <div class="page-intro">
      <div>
        <span class="eyebrow">系统配置</span>
        <h1>全局设置</h1>
        <p>治理、画质、入库、巡检、服务与机器人。网盘输出 / 命名 / 目录等配置在「我的网盘」内维护。</p>
      </div>
      <button class="btn gray" data-icon="refresh" :disabled="busy" @click="loadAll">刷新</button>
    </div>
    <p v-if="message" class="section-note" role="status">{{ message }}</p>
    <p v-if="error" class="section-note">{{ error }}</p>

    <!-- 治理策略 -->
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="bookmark">治理策略</h3>
        <button class="btn gray" :disabled="busy" @click="saveStrategy">保存治理策略</button>
      </div>
      <div class="meta-row">
        <span class="k">决策模型</span>
        <span class="v">
          <span class="segmented">
            <button
              v-for="o in decisionOptions"
              :key="o.value"
              :class="{ active: strategy.decision === o.value }"
              @click="strategy.decision = o.value"
            >
              {{ o.label }}
            </button>
          </span>
        </span>
      </div>
      <div class="meta-row">
        <span class="k">配对策略</span>
        <span class="v">
          <span class="segmented">
            <button
              v-for="o in matchOptions"
              :key="o.value"
              :class="{ active: (strategy.match_strategy || 'title_year') === o.value }"
              @click="strategy.match_strategy = o.value"
            >
              {{ o.label }}
            </button>
          </span>
        </span>
      </div>
      <div class="meta-row">
        <span class="k">多季合集保护</span>
        <span class="v">
          <span class="segmented">
            <button
              v-for="o in protectOptions"
              :key="o.value"
              :class="{ active: (strategy.multi_season_protect || 'compare') === o.value }"
              @click="strategy.multi_season_protect = o.value"
            >
              {{ o.label }}
            </button>
          </span>
        </span>
      </div>
      <div class="meta-row">
        <span class="k">平局保留本地</span>
        <span class="v">
          <label class="switch"
            ><input v-model="strategy.tie_keep_local" type="checkbox" /><span class="slider"></span
          ></label>
        </span>
      </div>
      <div class="meta-row">
        <span class="k">剧集分享达标率阈值</span>
        <span class="v">
          <input
            v-model.number="strategy.season_replace_ratio"
            type="range"
            min="0.5"
            max="1"
            step="0.05"
            style="width: 220px"
          />
          {{ Math.round((Number(strategy.season_replace_ratio) || 0.9) * 100) }}%
        </span>
      </div>
      <div class="meta-row">
        <span class="k">豁免关键词（每行一个）</span>
        <span class="v"><textarea v-model="exemptText" rows="3" style="width: 100%"></textarea></span>
      </div>
    </div>

    <!-- 特别篇 -->
    <div class="panel">
      <h3 data-icon="film">特别篇（S00）策略</h3>
      <div class="meta-row">
        <span class="k">处理方式</span>
        <span class="v">
          <span class="segmented">
            <button
              v-for="o in specialOptions"
              :key="o.value"
              :class="{ active: (strategy.special_action || 'compare') === o.value }"
              @click="strategy.special_action = o.value"
            >
              {{ o.label }}
            </button>
          </span>
        </span>
      </div>
    </div>

    <!-- 画质对比规则 -->
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="scale">画质对比规则</h3>
        <button class="btn gray" :disabled="busy" @click="saveCover">保存画质规则</button>
      </div>
      <div v-for="(rule, index) in cover" :key="text(rule.key)" class="cw-settings-group">
        <div class="row-between">
          <strong>{{ text(rule.label) }}</strong>
          <label class="switch"
            ><input v-model="rule.enabled" type="checkbox" /><span class="slider"></span
          ></label>
        </div>
        <p class="hint">{{ text(rule.desc) }}</p>
        <div v-for="(tier, tierIndex) in strings(rule.tiers)" :key="tier" class="cw-file-row">
          <span class="cw-file-name">{{ tier }}</span>
          <span class="cw-file-meta">
            <button class="btn gray sm" :disabled="tierIndex === 0" @click="moveTier(rule, tierIndex, tierIndex - 1)">↑</button>
            <button
              class="btn gray sm"
              :disabled="tierIndex >= strings(rule.tiers).length - 1"
              @click="moveTier(rule, tierIndex, tierIndex + 1)"
            >
              ↓
            </button>
          </span>
        </div>
        <template v-if="strings(rule.groups).length !== undefined">
          <div v-for="(group, groupIndex) in strings(rule.groups)" :key="group" class="cw-file-row">
            <span class="cw-file-name">{{ group }}</span>
            <span class="cw-file-meta">
              <button class="btn gray sm" :disabled="groupIndex === 0" @click="moveGroup(rule, groupIndex, groupIndex - 1)">↑</button>
              <button
                class="btn gray sm"
                :disabled="groupIndex >= strings(rule.groups).length - 1"
                @click="moveGroup(rule, groupIndex, groupIndex + 1)"
              >
                ↓
              </button>
              <button class="btn gray sm" @click="removeGroup(rule, groupIndex)">×</button>
            </span>
          </div>
          <div v-if="rule.key === 'release_group'" style="margin-top: 6px">
            <input v-model="newGroup" placeholder="新增发布组，例如 CMCTV" maxlength="40" style="width: 220px" />
            <button class="btn gray sm" @click="addGroup(rule)">添加</button>
          </div>
        </template>
      </div>
    </div>

    <!-- 入库监控 -->
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="activity">入库监控</h3>
        <button class="btn gray" :disabled="busy" @click="saveIngest">保存入库监控</button>
      </div>
      <div class="meta-row">
        <span class="k">启用</span>
        <span class="v"><label class="switch"><input v-model="ingest.enabled" type="checkbox" /><span class="slider"></span></label></span>
      </div>
      <div class="meta-row"><span class="k">模式</span><span class="v"><input v-model="ingest.mode" /></span></div>
      <div class="meta-row"><span class="k">轮询间隔（分钟）</span><span class="v"><input v-model.number="ingest.interval_min" type="number" min="1" /></span></div>
      <div class="meta-row"><span class="k">对账周期（小时）</span><span class="v"><input v-model.number="ingest.reconcile_hours" type="number" min="1" /></span></div>
      <div class="meta-row"><span class="k">回调地址</span><span class="v"><code>{{ text(ingest.webhook_token) }}</code></span></div>
    </div>

    <!-- 双库定时巡检 -->
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="scale">双库定时巡检</h3>
        <button class="btn gray" :disabled="busy" @click="saveAuto">保存巡检设置</button>
      </div>
      <div class="meta-row"><span class="k">启用</span><span class="v"><label class="switch"><input v-model="auto.enabled" type="checkbox" /><span class="slider"></span></label></span></div>
      <div class="meta-row"><span class="k">间隔（小时）</span><span class="v"><input v-model.number="auto.interval_hours" type="number" min="1" /></span></div>
      <div class="meta-row"><span class="k">有清理时通知</span><span class="v"><label class="switch"><input v-model="auto.notify_when_clean" type="checkbox" /><span class="slider"></span></label></span></div>
      <div class="meta-row"><span class="k">仅在变化时记录</span><span class="v"><label class="switch"><input v-model="auto.only_on_change" type="checkbox" /><span class="slider"></span></label></span></div>
      <div class="meta-row"><span class="k">最近状态</span><span class="v">{{ text(auto.last_status) }}</span></div>
    </div>

    <!-- 服务配置 -->
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="server">服务配置</h3>
        <button class="btn gray" :disabled="busy" @click="saveConfig">保存服务配置</button>
      </div>
      <div class="meta-row"><span class="k">Emby 地址</span><span class="v"><input v-model="config.emby_host" style="width: 100%" /></span></div>
      <div class="meta-row"><span class="k">Emby Key</span><span class="v">{{ text(config.emby_key) }}（只读）</span></div>
      <div class="meta-row"><span class="k">TMDB Key</span><span class="v">{{ text(config.tmdb_key) }}（只读）</span></div>
      <div class="meta-row"><span class="k">上游地址</span><span class="v"><input v-model="config.tgto_url" style="width: 100%" /></span></div>
      <div class="meta-row"><span class="k">本地库路径</span><span class="v"><input v-model="config.emby_local_path" style="width: 100%" /></span></div>
      <div class="meta-row"><span class="k">分享库路径</span><span class="v"><input v-model="config.emby_share_path" style="width: 100%" /></span></div>
      <div class="meta-row"><span class="k">HTTP 代理</span><span class="v"><input v-model="config.http_proxy_url" style="width: 100%" placeholder="留空为不使用" /></span></div>
    </div>

    <!-- 机器人 -->
    <div class="panel">
      <h3 data-icon="send">Telegram 机器人</h3>
      <div v-if="!Object.keys(bot).length" class="list-empty">未配置机器人</div>
      <div v-for="[key, value] in Object.entries(bot)" :key="key" class="meta-row">
        <span class="k">{{ key }}</span>
        <span class="v">{{ shown(value) }}</span>
      </div>
    </div>
  </section>
</template>
