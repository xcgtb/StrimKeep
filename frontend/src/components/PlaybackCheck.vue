<script setup lang="ts">
import { ref } from 'vue';
import { Play, Copy, CircleCheck } from '@lucide/vue';
import { writeData, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useUi } from '../stores/ui';
const props = defineProps<{ assets: Data[] }>();
const selected = ref(''),
  result = useRequest<Data>(),
  ui = useUi();
function check() {
  if (!selected.value) return;
  return result.run(() =>
    writeData(`/api/cloud/assets/${encodeURIComponent(selected.value)}/playback/check`),
  );
}
async function copy(value: unknown) {
  try {
    await navigator.clipboard.writeText(String(value));
    ui.notify('播放链接已复制', 'success');
  } catch {
    ui.notify('复制失败，可在下方选择链接后复制', 'error');
  }
}
</script>
<template>
  <section class="panel workflow-config-card playback-check">
    <header class="section-heading">
      <h3><Play :size="17" />验证播放链路</h3>
    </header>
    <p class="muted-text">只读核验已生成的 STRM、签名和 115 源文件解析，不启动视频下载。</p>
    <label
      >选择已生成的资源<select
        v-model="selected"
        :disabled="result.loading.value"
        @change="
          result.data.value = undefined;
          result.error.value = '';
        "
      >
        <option value="">请选择播放引用</option>
        <option
          v-for="asset in props.assets.filter((a) => a.state === 'active')"
          :key="String(asset.id)"
          :value="String(asset.id)"
        >
          {{ String(asset.path).split('/').pop() }}
        </option>
      </select></label
    ><button class="button" :disabled="!selected || result.loading.value" @click="check">
      {{ result.loading.value ? '正在核验…' : '核验源文件与播放解析' }}
    </button>
    <p v-if="result.error.value" class="inline-error">{{ result.error.value }}</p>
    <div v-if="result.data.value" class="playback-check-result">
      <strong
        ><CircleCheck :size="16" />{{
          result.data.value.mode === '302' ? '302 解析通过' : '受控代理解析通过'
        }}</strong
      >
      <p>{{ result.data.value.message }}</p>
      <div
        v-for="(label, key) in { internal_link: 'STRM 播放链接', external_link: '外网播放链接' }"
        :key="key"
      >
        <template v-if="result.data.value[key]"
          ><label>{{ label }}<input :value="result.data.value[key]" readonly /></label
          ><button class="text-link" @click="copy(result.data.value[key])">
            <Copy :size="13" />复制{{ label }}
          </button></template
        >
      </div>
    </div>
    <p v-if="!props.assets.length" class="muted-text">生成首个 STRM 后即可在这里核验。</p>
  </section>
</template>

<style scoped>
.playback-check,
.playback-check-result {
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
}
.playback-check > *,
.playback-check-result > *,
.playback-check label {
  min-width: 0;
  max-width: 100%;
}
.playback-check select,
.playback-check input {
  display: block;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  text-overflow: ellipsis;
}
.playback-check > .button {
  width: 100%;
  white-space: normal;
}
.playback-check p,
.playback-check-result .text-link {
  overflow-wrap: anywhere;
}
</style>
