# KBgm

为 KikoPlay 生成 Bangumi Archive 动画资料包。GitHub Actions 下载上游全量快照、筛选动画和剧集、压缩并发布到 GitHub Releases。服务器自行拉取文件和导入数据库；本仓库不连接服务器，也不保存服务器或数据库凭据。

## 定时运行

- 每周三 **21:00（UTC+8 / 北京时间）** 检查一次最新 dump。
- Actions 使用 UTC 表达式 `0 13 * * 3`。
- 上游版本未变化时跳过下载和构建，但会修复可能尚未提交的发布清单。
- 可在 Actions → **Publish Bangumi dump** → **Run workflow** 手动运行。仅默认分支允许发布。
- 定时任务由 GitHub 调度，可能延迟，不保证精确到点开始。失败后可以手动重跑。

## 增量规则

沿用 KikoPlayServer 的提取格式和筛选逻辑：仅提取 `type=2` 的动画，要求名称和有效开播日期，取所选动画的正篇及特别篇剧集。

这里的“增量”是**按开播日期筛选的重叠快照**，不是按资料修改时间计算的 diff。它不会覆盖窗口外老番的新集、资料修正或删除；既有服务器导入脚本对已有资料的更新策略也保持不变。

`config.json` 定义两个参数：

| 参数 | 初始值 | 用途 |
| --- | --- | --- |
| `bootstrap_since` | `2026-07-01` | 首次发布的筛选下界，含当天 |
| `buffer_days` | `90` | 后续从上次成功发布的 dump 日期向前回看 90 天 |

首次日期取自迁移时已有的本地处理版本 `dump-2026-09-29.210336Z` 向前 90 天，只定义新仓库的提取范围，不表示服务器已导入该版本。

手动运行可填写 `since`（`YYYY-MM-DD`）扩大窗口。为防止遗漏数据，不允许填入晚于自动下界的日期；已发布的同版本不会被覆盖。需要首次全量提取时，可在首次运行前指定足够早的日期，例如 `0001-01-01`。

## Release 产物

每个上游版本对应一个 Release 和同名 tag，例如 `dump-2026-09-29.210336Z`。每个 Release 包含：

- `bgm_extracted.zip`：五个 TSV 文件和 `dump_version.txt`，兼容 KikoPlayServer 的 `run_import.sh` / `import_remote.py`。
- `manifest.json`：版本链、筛选范围、SHA-256、文件大小、条目计数和格式版本。

ZIP 内文件：

```text
anime_profile.tsv
anime_info.tsv
anime_source.tsv
anime_tag.tsv
pool_info.tsv
dump_version.txt
```

清单主要字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 清单格式版本，目前为 1 |
| `dump_version` | 当前上游 dump 版本，也是 Release tag |
| `previous_version` | 上次成功发布版本；首包为 `null` |
| `since` | 本包动画开播日期下界，包含当天 |
| `archive.name` / `archive.size` / `archive.sha256` | 增量 ZIP 名称、字节数和 SHA-256 |
| `source` | 上游压缩包地址、大小及下载校验信息 |
| `counts` | 各 TSV 的逻辑记录数 |

发布成功后，Actions 将清单提交到默认分支的 `data/latest.json`，用于查看最新发布状态并形成真实的数据更新记录。**Release 中的已发布清单是生产进度的依据**，该文件不是服务器的导入记录。

所有历史版本包保留，不自动清理。服务器应按 `previous_version` 链补齐未导入版本；`latest` 只适合发现最新版本。发现缺包或链断裂时应停止并补数据，不能静默跳过。真正的服务器拉取任务在 KikoPlayServer 中另行实现。

## 发布失败与重跑

1. 完成下载、校验、提取和压缩后，创建带本项目标记的 draft Release。
2. 上传 ZIP 和清单；全部成功后才公开该 Release。
3. 最后提交 `data/latest.json`。未完成的 draft 不计入已发布版本。

失败重跑会恢复本工作流创建的同版本 draft，已公开的版本不会被覆盖。如果 Release 已发布而清单提交失败，下次运行会从 Release 恢复本地清单。对同名、非本工作流创建的 draft 会报错，不擅自删除。

工作流使用并发组避免同时发布，不取消已经运行的发布任务。工作流只需要当前仓库的 `GITHUB_TOKEN` 和 `contents: write` 权限，不需要另外设置个人访问令牌。默认分支需要允许工作流提交 `data/latest.json`；分支保护若阻止提交，Release 仍会保留，工作流会显示失败。

公开仓库定时任务有 GitHub 的 60 天无活动停用规则。每次发布后的清单提交提供实际仓库更新，但上游长期停止发布或任务长期失败仍需关注。已经停用时，在 Actions 页面点击 **Enable workflow**。服务器拉取任务应检测产物长期未更新的情况。

## 本地检查

Python 3.11：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用小型合成 dump 和模拟 GitHub API，不下载全量上游数据，也不创建真实 Release。

本地运行生产脚本时，每次构建使用新的 `--output` 目录；已有 ZIP 或清单的目录会被拒绝覆盖。Actions 每次运行使用新的工作区。

已有解压的 dump 可仅运行提取：

```bash
python scripts/extract_local.py /path/to/dump ./dist/extracted --since 2026-07-01
```

## 来源

- 上游数据：[bangumi/Archive](https://github.com/bangumi/Archive)。数据的使用和再分发需遵循上游适用要求。
- 提取器来自 KikoPlayServer `tools/import_bgm/extract_local.py`，保留名称 ID、弹幕池 ID 与 TSV 字段的兼容性。
- 调度行为：[GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
