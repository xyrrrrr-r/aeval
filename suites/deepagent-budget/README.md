# deepagent-budget — deepagent 的预算套件

本套件与 e2e-hello 同一任务形态（把 `hello` 写进
`/workspace/result`），但驱动要求与证据槽位面向 ACP 适配器 deepagent：

- `driver.require: [acp_stdio, shell, file_tools]` —— deepagent 提供的三项。
  基座不含任何 agent 特性，能力要求一律由套件自己声明，因此这里不再需要
  `remove.require` 补丁；e2e-hello 自己也只要求任务需要的两项
  （`[acp_stdio, shell]`），它与 deepagent 的差异现在落在**会话记录槽位**
  上：e2e-hello 采集 `dsh_session`，deepagent 产 `agent_session_record`，
  组合期照样精确拒绝（槽位门，而不是能力门）；
- `driver.session_record: agent_session_record` —— 会话记录走通用槽位；
  deepagent 的官方会话记录是 Harbor ACP runner 的 `acp-summary.json`
  （session id、stop reason、token usage、instruction），由适配器的
  `read_session_record()` 采集；完整事件流经 Harbor 的 ATIF 转换进
  canonical transcript；
- **无预算**：deepagent 目前 `budget_enforcement: none`（broker 非 OpenAI
  兼容），带预算的套件会拒绝它 —— 计量目前依赖 OpenAI façade 控制栈，尚未接入。

封存后本套件的 jobs/*.yaml 即封存证据，字节不再改动。
