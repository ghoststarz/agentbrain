# diary-note · Hermes 桌面插件

把日记动作显示成会话里的一行彩色提示——形态对齐内置的「自我改进回顾」行（图标 + 标签 + 灰色说明），不是 toast、不需要点开。

## 它是怎么工作的

Hermes 桌面端有一个 `transcript.directives` 贡献区：插件注册一个 directive 名，模型在回复里写 `::<name>{...}`，那一行就渲染成插件的组件（核心的 `::preview{file="…"}` 走的是同一套机制）。

所以这个插件没有后端、不发请求、不读文件、不轮询——一个单文件、100 行、只 import `react/jsx-runtime`。

## 用法

模型侧（写完日记后附一行）：

```
::diary-note{ledger="public" time="16:11" text="把 AgentBrain 收进一个真仓库，踏实又有点慌"}
```

渲染结果：`📔 公账已记 16:11 · 把 AgentBrain 收进一个真仓库，踏实又有点慌`

| 属性 | 规则 |
|---|---|
| `ledger` | `public` → 公账已记，`private` → 私账已记，其它/缺失 → 日记已记 |
| `time` | 必须匹配 `HH:MM`，否则丢弃 |
| `text` | 折叠空白、截断到 120 字（超出加 `…`） |

三个属性都是模型产出的不可信输入，插件自己校验；`render` 整段 `try/catch`，异常退化成纯文本「日记已记」。属性体不能包含 `{}`。

## 安装

桌面插件是**应用级**的（一份对所有 profile 生效），放在 `<hermes home>/desktop-plugins/<id>/plugin.js`。独立目录形态默认启用，不需要在 Settings 里勾。

```bash
# 目录联接（推荐：仓库是唯一真源）
powershell -NoProfile -Command "New-Item -ItemType Junction -Path \"$env:LOCALAPPDATA\hermes\desktop-plugins\diary-note\" -Target \"<repo>\integrations\hermes\diary-note\""
```

生效方式：命令面板 → **Reload desktop plugins**，或重启客户端。加载后旧消息里的 directive 会重新渲染。

## 边界

- 这是 **desktop-plugin**（纯 UI），不是 agent-plugin：不带 `plugin.yaml`、不注册任何工具、不出现在 agent 会话里。
- 内置那条金色「自我改进回顾」行是核心的 `review.summary` 事件，插件发不出来（命名空间被核心占住）；本插件用 directive 在**消息内**复刻同样的观感，不改 Hermes 源码。
- 样式用 CSS 变量 + 十六进制 fallback：宿主变量缺失时退成纯色，不会变成隐形文字。
