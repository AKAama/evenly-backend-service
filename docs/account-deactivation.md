# 账号注销（软注销）产品与技术设计

最后更新：2026-07-22  
状态：**已实现（2026-07-22）** — 迁移 `20260722_0025`，API + iOS 注销流 + 控制台

## 1. 目标

用户注销账号后：

- **账务事实保留**：账单、分摊、确认、历史结算、成员关系仍在。
- **账号不可用**：不能登录、不能写操作、对外不暴露「逻辑删除」实现细节。
- **Owner 可交接**：有其他正式成员时移交；仅自己时账本进入**归档（悬空账本）**，由平台管理。

**不做**：账号恢复、同邮箱自动合并历史账号、物理删除共享账本中的账单。

---

## 2. 已锁定产品规则

| # | 规则 |
| --- | --- |
| 1 | 注销前处理 Owner：弹窗告知待移交账本数；可手动指定新 Owner；不指定则系统移交给**除 Owner 外最早进入的正式成员**；确认后展示移交结果表，提醒截图；同时通知新 Owner。 |
| 2 | 历史账单中此人仍参与结算/展示（成员仍算）。 |
| 3 | 展示名：`{原 display_name 或 username}（已注销）`。 |
| 4 | 平台管理员「删人」= 同一套软注销，不是硬删。 |
| 5 | 再注册无自动合并；用户不可感知底层是软删。邮箱立即释放；用户名冷却 **90 天** 后释放。无恢复账号。 |
| 6 | **仅自己的账本 → 归档**（不解散）。平台可管理全部「悬空账本」。 |

### 补充约定

| 项 | 约定 |
| --- | --- |
| 邮箱 | 注销成功后立即释放，可被新账号注册（新 `user_id`，无关联）。 |
| 用户名冷却 | **90 天**，配置项 `username_release_days = 90`。 |
| 对外话术 | 「账号已注销」；不出现「逻辑删除 / 软删除 / 保留数据」等实现用语。 |
| 登录失败 | 统一「账号或密码错误」类文案（防枚举）。 |
| 默认继承人 | **除当前 Owner 外，合格正式成员中 `ledger_members.created_at` 最早者**（不是「排序第二名」字面义）。 |

---

## 3. Owner 与归档流程

### 3.1 预检 `GET /users/me/deactivation-preview`

```json
{
  "owned_ledgers_requiring_transfer": [
    {
      "ledger_id": "...",
      "ledger_name": "日本之旅",
      "member_count_registered_active": 3,
      "default_successor": {
        "user_id": "...",
        "display_name": "Sylvia",
        "username": "sylvia"
      },
      "candidates": []
    }
  ],
  "owned_ledgers_to_archive": [
    {
      "ledger_id": "...",
      "ledger_name": "只有我",
      "action": "archive",
      "reason": "sole_registered_member"
    }
  ],
  "membership_ledger_count": 5
}
```

**默认继承人（「除 Owner 外最早进入」）：**

1. 仅 **正式成员**（`ledger_members.user_id IS NOT NULL`）。
2. 排除当前 Owner。
3. 排除已注销用户（`users.status = deactivated`）。
4. 成员自身为 active（若有成员 status）。
5. 在以上集合中取 **`created_at ASC` 的第一条**（最早加入者）。

### 3.2 确认弹窗（客户端）

1. 文案：你是 N 本账本的 Owner，其中 M 本需移交，K 本将归档。  
2. 需移交的每本账本：可选新 Owner，或「使用系统默认（xxx）」。  
3. 将归档的账本：说明仅你一人，注销后账本进入归档，由平台保管；需勾选知晓。  
4. 主按钮：确认注销（可二次确认）。

### 3.3 执行 `POST /users/me/deactivate`

```json
{
  "owner_transfers": [
    { "ledger_id": "...", "new_owner_id": "..." }
  ],
  "confirm": true
}
```

- 未传 `new_owner_id` 的「需移交」账本 → 用默认继承人。  
- 「仅自己」账本 → 不要求 `new_owner_id`，执行 **归档**。  
- 服务端校验候选人；失败则整单回滚。  
- 事务内：移交 / 归档 → 用户 deactivated → 脱敏 → 审计 → 通知新 Owner。

### 3.4 结果页（必须）

| 账本 | 结果 |
| --- | --- |
| 日本之旅 | 已移交给 Sylvia (@sylvia) |
| 个人备忘 | 已归档（仅你一人） |

文案：请截图保存。注销后你将无法再登录查看。

通知新 Owner（APNs）：

> 「{原 Owner 展示名} 已将账本「{账本名}」的管理权移交给你。」

归档账本**不**发「新 Owner」通知（无活跃 Owner）。

### 3.5 仅 Owner 一人的账本 → 归档（悬空账本）

| 项 | 约定 |
| --- | --- |
| 行为 | **归档**，不解散、不删账单 |
| `ledgers` 状态 | 如 `status = archived`（或 `archived_at` 非空） |
| `owner_id` | 可仍指向已注销用户，或置空若模型允许；推荐 **保留 owner_id 指向已注销用户** + `status=archived`，便于审计 |
| App 用户 | 注销后不可见（账号已不可登录）；其他用户本就不会在该账本内 |
| 平台 | 可列表、查看、管理全部 **悬空账本** |

**悬空账本定义（平台侧）：**

```text
ledger 已归档
  且 正式成员中「未注销」人数 = 0
  （典型：仅一名正式成员且该成员已注销）
```

平台能力（控制台，建议）：

| 能力 | 说明 |
| --- | --- |
| 列表筛选 | 「归档 / 悬空账本」 |
| 只读总览 | 成员、账单、结余（与现有 admin ledger overview 一致） |
| 可选后续 | 指定新 Owner 解档、或管理员解散（二期，非注销必做） |

注销当时：**只做归档**；解档/指派新 Owner 属平台运维后续能力。

### 3.6 非 Owner 的成员身份

- 保留 `ledger_members`。  
- 用户 `status=deactivated`。  
- 展示：`张三（已注销）`。  
- 仍计入历史分摊与结算。  
- 新操作选人时过滤 deactivated。

---

## 4. 数据模型

### 4.1 `users` 新增

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `active` / `deactivated` | 默认 `active` |
| `deactivated_at` | timestamptz null | 注销时间 |
| `display_name_frozen` | string null | 注销时冻结展示名 |
| `username_held_until` | timestamptz null | 用户名可再注册时间（+90 天） |

### 4.2 `ledgers` 新增

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `active` / `archived` | 默认 `active` |
| `archived_at` | timestamptz null | 归档时间 |
| `archive_reason` | string null | 如 `sole_owner_deactivated` |

### 4.3 注销时脱敏

| 字段 | 处理 |
| --- | --- |
| `email` | 占位 `deleted+{user_id}@invalid.local`；**原邮箱立即释放** |
| `password_hash` | 作废 |
| `auth_identities` | 删除/断开，释放 Apple subject |
| `avatar_url` | 清空并尽量删 COS |
| `push_devices` | 全部失效 |
| `username` | 保留至 `username_held_until`（+90 天）；到期改名释放 |
| 展示名 | 冻结，API 拼「（已注销）」 |
| `badge` | 可清空 |

**禁止**：`DELETE FROM users`；禁止删除其关联 expense / split（归档账本内账单同样保留）。

### 4.4 展示

```text
display_label =
  if status == deactivated:
    f"{display_name_frozen or username}（已注销）"
  else:
    display_name or username
```

---

## 5. 平台管理员

| 能力 | 说明 |
| --- | --- |
| 注销用户 | `POST /admin/users/{id}/deactivate`，同软注销 |
| 移交 | 有其他成员时指定或默认继承人；仅自己 → 归档 |
| 悬空账本 | 列表/筛选 `archived` + 无活跃正式成员；只读管理 |
| 话术 | 「注销账号」「归档账本」，无硬删入口 |

审计：`user.deactivate` / `user.deactivate_admin` / `ledger.archive`，含移交与归档明细。

---

## 6. 登录 / 注册 / 用户名

| 场景 | 行为 |
| --- | --- |
| 已注销登录 | 失败 |
| 原邮箱注册 | 允许，新用户 |
| 原用户名 | `now < username_held_until` → 不可用；90 天后可注册 |
| 冷却释放 | 定时或注册时惰性改旧 username |

---

## 7. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/users/me/deactivation-preview` | 预检：待移交 / 待归档 |
| POST | `/users/me/deactivate` | 执行注销 + 移交/归档 |
| DELETE | `/users/me` | 废弃硬删，改为走 deactivate 或 410 |
| POST | `/admin/users/{id}/deactivate` | 平台注销 |
| GET | `/admin/ledgers?status=active\|archived` | 账本列表；`is_orphan` 标签表示归档且无存活正式成员（不再单独筛 orphan） |

`deactivate` 响应示例：

```json
{
  "transfers": [
    {
      "ledger_id": "...",
      "ledger_name": "日本之旅",
      "action": "transfer",
      "new_owner": { "user_id": "...", "display_name": "Sylvia", "username": "sylvia" }
    },
    {
      "ledger_id": "...",
      "ledger_name": "个人备忘",
      "action": "archive",
      "new_owner": null
    }
  ]
}
```

---

## 8. 客户端流程（iOS）

1. 设置 → 注销账号  
2. preview → 说明移交数 / 归档数  
3. 可改继承人；确认知晓归档  
4. deactivate → 结果表（移交谁 / 已归档）+ 请截图  
5. 清会话 → 登录页  
6. 全局展示 `xxx（已注销）`  

---

## 9. 明确不做

- 账号恢复  
- 新注册合并旧账  
- 向用户解释软删实现  
- 注销时解散/硬删共享或独享账本中的账单（独享只归档）  

---

## 10. 决策记录

| 日期 | 决策 |
| --- | --- |
| 2026-07-22 | 用户名冷却 **90 天** |
| 2026-07-22 | 仅自己的账本 **归档**；平台管理悬空账本 |
| 2026-07-22 | 默认继承人 = **除 Owner 外最早进入的正式成员**（非「排序第二」字面） |
