# 模块依赖图（import 环与懒加载）

由 `tools/import_graph.py` 生成。判据：**顶层互相 import = 真环**；**一边是函数内 import = 懒加载绕环**。

## 一、真环（顶层互相 import，1 条）

| A | B | 说明 |
| --- | --- | --- |
| media | audio | `media` 用函数内 import 回指 `audio`（懒加载） |

## 二、函数内懒加载（42 处）

这些不是环，只是"用到时才 import"；拆顶层环时可以顺手提升到顶层。

| 模块 | 懒加载目标 | 行号 |
| --- | --- | --- |
| core | fsutil | 33 |
| emby | emby_connection | 54 |
| main | channels_api | 491 |
| main | console_api | 479 |
| main | cover_service | 493 |
| main | emby_connection | 701 |
| main | explore_api | 485 |
| main | library_name | 132 |
| main | media | 789 |
| main | network | 143, 687 |
| main | offline_api | 483 |
| main | resource_discovery | 487 |
| main | share_api | 489 |
| main | store | 286 |
| main | transfer_api | 481 |
| media | audio | 593 |
| metadata_files | fsutil | 76 |
| metadata_files | strm_options | 101, 127, 144 |
| native_media_info | fsutil | 95 |
| organizer | cards | 553 |
| organizer | cover_service | 114 |
| organizer | deep_delete | 119 |
| organizer | untyped_cleanup | 698, 2335 |
| outputs | core | 20, 94 |
| phase5_api | notifications | 140 |
| share_api | cloud115 | 148 |
| share_service | core | 273, 865 |
| share_service | organize_options | 565 |
| strm_options | cloud115 | 74 |
| strm_options | fsutil | 63 |
| strm_options | media | 75 |
| sync | core | 88, 94, 130, 171, 217, 317 |
| sync | fsops | 699 |
| sync | media | 523 |
| sync | metadata_files | 522 |
| sync_worker | cloud115 | 23 |
| sync_worker | network | 24 |
| sync_worker | organizer | 25 |
| sync_worker | store | 22 |
| telegram_bot | share_service | 177 |
| transfer | core | 62 |
| upgrade | core | 348 |

## 三、建议的拆分顺序

1. **先提懒加载**：`sync.py`、`main.py` 里那些只为绕开循环而写的函数内 import，若目标模块并不回指它，直接提到顶层（零风险，靠测试兜底）；
2. **再拆真环**：把环里"被两边共同需要"的小工具（路径、命名、序列化）抽成独立模块，让双方都依赖它，而不是互相依赖；
3. **最后动 `organizer.py`**：它是中心节点，只有前面几批把周边理顺后，才能把它的职责按「扫描 / 识别 / 发布 / 清理」拆开，每拆一块跑一次全量回归。
