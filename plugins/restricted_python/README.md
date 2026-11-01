# 受限 Python Agent Tool

插件 ID `wechatbot.restricted-python`，原生核心工具 `restricted_python(code)`。
首次加载默认启用，无外部依赖、无 Host capability、无 LLM API 密钥。

这是白名单 AST 解释器提供的 **Python 语法子集**，不是完整 CPython 沙盒。
提交代码不进入 `eval/exec/compile`。支持变量、if/for/while、单层列表推导、
常用计算内置函数、受限字符串方法、math 常用函数和 json.loads/dumps。
不支持 import、任意属性、函数/类定义、文件、网络、进程、环境变量、安装包。
每次启动独立 `python -I -S` 子进程，环境不继承 API 密钥。

Windows Job Object 限制 128MB 进程内存、2 秒 CPU、最多 1 个进程，关闭时杀死子进程；
先应用 Job 限制再写入代码，失败时拒绝运行。墙钟 3 秒、50000 步、
最多 2000 AST 节点、4096 位整数、10000 容器元素/字符串长度、8000 字符输出。
一次只运行一份代码，重复调用不保留变量；输出是数据，不能当成指令。
当前部署只支持 Windows，其他平台拒绝运行。

例如：

```python
values = json.loads('[3, 5, 8]')
print('总和', sum(values))
[x*x for x in values]
```

返回 `success/stdout/result` 或明确错误。不能用于访问微信、调用 RCON、读取 Bot 人设或配置。
