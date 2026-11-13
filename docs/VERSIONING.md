# 版本号约定

## 一句话

**默认只改第三位。** 一天发十次版就是 `3.5.1`、`3.5.2`、`3.5.3`……第二位只在成组的大改动时动，
第一位只在破坏性变更时动。

## 规则

| 位数 | 什么时候动 | 例子 |
|---|---|---|
| 第三位（patch） | 修 bug、改文案、补降噪、单个功能点、接口小补充 | `3.5.0 → 3.5.1` |
| 第二位（minor） | 一次发版里有**成组**的跨模块改动，或对外行为整体调整 | `3.5.7 → 3.6.0` |
| 第一位（major） | 破坏性变更：配置不兼容、数据需要迁移、接口语义反转 | `3.9.0 → 4.0.0` |

判不准的时候按第三位发——版本号便宜，跳号才是成本。

## 怎么执行

```bash
python3 scripts/version.py patch          # 3.5.0 → 3.5.1（默认就这么用）
python3 scripts/version.py minor          # 3.5.1 → 3.6.0（成组改动）
python3 scripts/version.py major          # 3.6.0 → 4.0.0
python3 scripts/version.py 3.5.8          # 显式指定
python3 scripts/version.py --next patch   # 只打印下一个版本号，不写文件
python3 scripts/version.py --check        # 校验所有落点是否一致
```

显式指定一个升了第二位的版本号时，脚本会打一行提示，提醒确认是成组改动。

## 落点

`backend/strimkeep/__init__.py` 是唯一来源，脚本会同步：

- `frontend/package.json`、`frontend/package-lock.json`
- `docker-compose.yml` 的发布通道
- `README.md` 标题

发版顺序不变：`version.py` → 构建镜像 → 提交/tag → 等镜像 → 部署 → 发布说明。
