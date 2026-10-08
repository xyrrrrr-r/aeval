# deepagent-hello — deepagent 的冒烟套件

本套件与 e2e-hello 同一任务形态（把 `hello` 写进
`/workspace/result`），但驱动要求与证据槽位面向 ACP 适配器 deepagent：

- `driver.require: [acp_stdio, shell, file_tools]` —— deepagent 提供的三项
  （e2e-hello 还要 `sdk_jsonrpc`，deepagent 被正确拒绝，那是能力门在工作）；
- `driver.session_record: agent_session_record` —— 会话记录走通用槽位；
  deepagent 的官方会话记录是 Harbor ACP runner 的 `acp-summary.json`
  （session id、stop reason、token usage、instruction），由适配器的
  `read_session_record()` 采集；完整事件流经 Harbor 的 ATIF 转换进
  canonical transcript；
- **无预算**：deepagent 目前 `budget_enforcement: none`（broker 非 OpenAI
  兼容），带预算的套件会拒绝它 —— 计量目前依赖 OpenAI façade 控制栈，尚未接入。

封存后本套件的 jobs/*.yaml 即封存证据，字节不再改动。
