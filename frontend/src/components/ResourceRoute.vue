<script setup lang="ts">
import { computed, watch } from 'vue';
import { readData, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
const props = defineProps<{
  share: boolean;
  organize: boolean;
  generate: boolean;
  target: string;
  archive: string;
  count: number;
  revision: number;
}>();
const state = useRequest<Data>();
const config = computed(() => object(state.data.value?.settings));
const output = computed(() =>
  String(config.value[props.share ? 'share_output_root' : 'output_root'] || '尚未配置'),
);
const emby = computed(() =>
  String(
    config.value[props.share ? 'share_emby_output_root' : 'emby_output_root'] ||
      '在入库核验页配置映射',
  ),
);
watch(
  () => props.revision,
  () => {
    void state.run((signal) => readData('/api/cloud/settings', signal));
  },
  { immediate: true },
);
</script>
<template>
  <section class="resource-route panel" aria-label="本次资源去向">
    <header class="section-heading">
      <div>
        <span class="eyebrow">{{ share ? 'SHARE TO LIBRARY' : 'RESOURCE ROUTE' }}</span>
        <h2>{{ share ? '分享来源，直接生成播放库' : '接收之后，资源去哪里' }}</h2>
      </div>
      <span class="module-tag">{{ count ? `已选 ${count} 项` : '范围待选择' }}</span>
    </header>
    <div class="route-destinations">
      <article>
        <span>01</span><small>{{ share ? '115 分享来源' : '115 接收目录' }}</small
        ><strong>{{
          share ? '仅访问当前分享中的所选内容' : target || '使用下方按资源类型保存的接收目录'
        }}</strong>
        <p>
          {{
            share
              ? '保留分享源，不占个人网盘转存空间。'
              : '磁力与 ed2k 提交后，还需等待115云下载完成。'
          }}
        </p>
      </article>
      <article>
        <span>02</span
        ><small>{{ share ? 'NAS 分享库 STRM' : organize ? '115 整理归档目录' : '后续整理' }}</small
        ><strong>{{
          share ? output : organize ? archive || '尚未配置归档目录' : '保留在接收目录，按需整理'
        }}</strong>
        <p>
          {{
            share
              ? '按已保存的命名、分类规则输出。'
              : organize
                ? '只整理本次转存范围，核实后命名归档。'
                : '可在影视整理页选择范围，再预览归档。'
          }}
        </p>
      </article>
      <article>
        <span>03</span
        ><small>{{
          share ? 'Emby 可见路径' : organize && generate ? 'NAS 个人库 STRM' : '处理结果'
        }}</small
        ><strong>{{
          share ? emby : organize && generate ? output : '在同一任务链查看接收回执'
        }}</strong>
        <p>
          {{
            share || (organize && generate)
              ? config.emby_refresh_enabled
                ? '生成后等待 Emby 实际入库回执。'
                : 'Emby 联动已关闭，生成后需手动扫描。'
              : '后台接受请求后，仍需核实下载或转存结果。'
          }}
        </p>
      </article>
    </div>
    <p v-if="state.error.value" class="inline-error">{{ state.error.value }}</p>
    <p
      v-if="config.generate_strm_enabled === false && (share || (organize && generate))"
      class="warning-text"
    >
      STRM 生成总开关已关闭，请在输出设置核对后再提交。
    </p>
  </section>
</template>
