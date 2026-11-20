上一轮输出没有通过服务端校验，错误原文是：

<ERROR>

只修格式，不许新增事实、不许改变已有内容的意思。逐条自查后再输出：

- 每个 `longterm_upsert` / `project_update` / `hot_update` / `handoff_upsert` / `diary_append` 都必须带**非空** `source_refs`，且只能引用本轮证据里真实出现过的 event_id。
- `diary_append` 的 `ledger` 只能是 `public` 或 `private`，`event_date` 只能是本轮允许的日期。
- `longterm_upsert` 的 `title` / `content` 不能为空，`note_type` 只能是 fact / decision / entity / moc。
- 只输出一个合法 JSON 对象，不要解释、不要代码块围栏。
