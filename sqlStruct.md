# 一、核心实体关系图（逻辑）

```
User
  ↓
Ledger (账本)
  ↓
LedgerMember
  ↓
Expense
  ↓
ExpenseSplit
  ↓
Settlement
```

---

# 二、表结构设计（生产可用）

---

## 1️⃣ users

用户表

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(100),
    avatar_url TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

说明：

* 用 UUID，不用自增
* password 只存 hash
* avatar 为可扩展字段

---

## 2️⃣ ledgers（账本）

```sql
CREATE TABLE ledgers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    owner_id UUID REFERENCES users(id) ON DELETE CASCADE,
    currency VARCHAR(10) DEFAULT 'CNY',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

说明：

* 一个账本有一个 owner
* currency 为未来多币种准备

---

## 3️⃣ ledger_members（账本成员）

这是关键表。

```sql
CREATE TABLE ledger_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id UUID REFERENCES ledgers(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    nickname VARCHAR(100),
    joined_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (ledger_id, user_id)
);
```

说明：

* 多人协作
* 每个账本成员唯一

---

## 4️⃣ expenses（支出）

```sql
CREATE TABLE expenses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id UUID REFERENCES ledgers(id) ON DELETE CASCADE,
    payer_id UUID REFERENCES users(id),
    title VARCHAR(255),
    total_amount NUMERIC(12,2) NOT NULL,
    note TEXT,
    expense_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

说明：

* total_amount 用 NUMERIC，不用 float
* expense_date 用 DATE

---

## 5️⃣ expense_splits（分摊表）⭐核心

不要只存“平均分摊”，要可扩展。

```sql
CREATE TABLE expense_splits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    expense_id UUID REFERENCES expenses(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id),
    amount NUMERIC(12,2) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (expense_id, user_id)
);
```

说明：

* 支持：

  * 平均分
  * 指定比例
  * 不参与成员
* amount 精确到分

---

## 6️⃣ settlements（结算记录）

用于记录“谁还了谁多少钱”

```sql
CREATE TABLE settlements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id UUID REFERENCES ledgers(id) ON DELETE CASCADE,
    from_user_id UUID REFERENCES users(id),
    to_user_id UUID REFERENCES users(id),
    amount NUMERIC(12,2) NOT NULL,
    note TEXT,
    settled_at TIMESTAMP DEFAULT NOW()
);
```

说明：

* 不删除历史
* 用于对账

---