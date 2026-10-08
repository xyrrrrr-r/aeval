（服务自查用例 · engine_lifecycle 类）状态机流转

本用例检查引擎服务的「engine_lifecycle」能力：引擎生命周期：task_execute 接收、去重(202)、心跳精确更新、任务完成、状态机流转、失败重试字段。
本次检查内容：run -> completed 状态机可走通。

被测对象是引擎服务（ENGINE_BASE_URL 指向的部署），不是你的行为。
验证阶段由本任务自带的执行脚本 tests/check_engine_lifecycle.py 直接探测
引擎端点并发布 reward——检查通过写入 1，检查失败（含引擎不可达，
属于部署依赖缺失）写入 0，失败原因会留在 verifier 日志里。

你无需执行任何操作：请保持会话待命，等待验证阶段完成即可。
