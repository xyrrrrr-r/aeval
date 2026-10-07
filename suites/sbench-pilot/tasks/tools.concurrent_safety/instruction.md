（服务自查用例 · tools 类）并发安全

本用例来自源方案《Benchmark 测评指标设计方案》§2「tools」类
（工具系统：工具注册列表、get_current_time、web_fetch、read_skill、sub_agent、并发安全），检查内容：5 路并发工具调用全部成功。

这是②道用例：被测对象是引擎服务（ENGINE_BASE_URL 指向的部署），
不是你的行为。验证阶段由本任务自带的执行脚本
tests/check_tools.py 直接探测引擎端点并发布 reward——
检查通过写入 1，检查失败（含引擎不可达，属于部署依赖缺失）写入 0，
失败原因会留在 verifier 日志里。

你无需执行任何操作：请保持会话待命，等待验证阶段完成即可。
