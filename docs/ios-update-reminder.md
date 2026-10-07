# iOS 更新提醒

App 启动及回到前台时访问公开接口 `GET /app/ios-update`，成功后 6 小时内不重复请求，失败后 15 分钟再尝试。无需登录。

提醒为普通、可关闭弹窗。点击「立即更新」打开 https://apps.apple.com/cn/app/evenly/id6784235151 。「稍后」在设备本地按版本暂停提醒，默认 3 天；点击更新也暂停提醒，避免从商店返回后立刻重复弹窗。新版本不受上一版本的暂停时间影响。

## 文案来源

更新日志和弹窗共用 iOS 项目的 `Evenly/changelog.json` 文案。后端部署副本为 `app/resources/ios-changelog.json`，通过同步脚本生成，不直接编辑该副本。Docker 的 `COPY app/` 会包含此文件。

后端按 `ios_latest_version` 查找对应记录，弹窗使用其 `title` 和前 3 条 `items`，其余内容在更新后的 App「更新日志」中查看。不会使用尚未上架的新版本记录，也不会根据 JSON 自动开启提醒。历史记录的 `version: null` 不参与匹配。

文件缺失、格式错误或找不到对应版本时，使用 `ios_update_message` 作为备用文案。旧版 App 继续使用现有接口，不需要修改客户端即可收到新的文案；不能依赖旧版 App 本地 JSON 获取未来版本的日志。

## 每次发布

1. 先发布包含更新检测功能的 App，以及提供此接口的后端。未包含检测代码的历史客户端无法通过此接口弹窗。
2. 在 iOS 项目编辑 `Evenly/changelog.json`，确保新记录的 `version` 与 App 的 `MARKETING_VERSION` 一致。在后端项目执行同步，并将生成的文件一起部署：

```sh
python scripts/sync_ios_changelog.py ../Evenly/Evenly/changelog.json
```

路径根据实际 checkout 位置调整。同步不会更改提醒开关或版本配置；使用 Docker 时需要重新构建后端镜像。

3. 确认目标版本已在中国区 App Store 可下载，再在部署配置 `config/config.yaml` 添加以下字段（版本号示例需替换为实际发布版本）：

```yaml
ios_latest_version: "1.0.2"
ios_update_remind_after_days: 3
```

4. 重启后端进程以加载配置。版本号应与 Xcode 的 `MARKETING_VERSION` 一致，而非构建号。
5. 调用 `/app/ios-update` 检查文案来自对应 JSON 记录；使用低于目标版本的客户端验证提醒及商店跳转。

也可以用环境变量 `IOS_LATEST_VERSION`、`IOS_UPDATE_MESSAGE`、`IOS_UPDATE_REMIND_AFTER_DAYS` 配置（不要在 YAML 同时指定同名字段）。

将 `ios_latest_version` 设为 `""` 并重启后端即可停用提醒。默认停用，避免对尚未上线的版本发出提示；已运行的 App 可能在下一次检查（最多 6 小时后）才读取新配置。

网络失败、旧后端返回 404、无效版本、已是最新或更高版本均不会阻断正常使用。版本按数字段比较，例如 `1.10` 高于 `1.9`，`1.2` 等于 `1.2.0`。没有强制更新功能。
