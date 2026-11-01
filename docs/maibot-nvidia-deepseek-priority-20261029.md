# NVIDIA 免费 DeepSeek 优先接入

## 官方核实

- [NVIDIA DeepSeek V4.1 Flash](https://build.nvidia.com/deepseek-ai/deepseek-v4.1-flash) 当前标有免费原型 API；模型为 `deepseek-ai/deepseek-v4.1-flash`，OpenAI 兼容地址为 `https://integrate.api.nvidia.com/v1`。
- [API 参考](https://docs.api.nvidia.com/nim/reference/nvidia-deepseek-v4_1-flash) 明确支持文本、图片、工具调用，因此预配置保留视觉能力。具体接口兼容性仍须密钥实测。
- [官方 FAQ](https://docs.api.nvidia.com/nim/docs/faq) 将免费额度定位于原型验证，不能承诺永久无限量。
- [生成密钥流程](https://docs.api.nvidia.com/nim/docs/api-quickstart)：登录 NVIDIA 开发者账号，在模型页面生成个人 API Key。

## 本次变更

- `config.json`：新增 NVIDIA DeepSeek 路由，优先级为 `0`；其余模型优先级降至 `100` 以后，保持现有已启用路由的相对顺序。缺少有效密钥，DeepSeek 暂未启用；当前运行进程和当前可用调用链仍未切换为 DeepSeek。
- `scripts/configure_nvidia_deepseek.py`：读取本地密钥文件或 `NVIDIA_API_KEY`，合成数据测试 Planner JSON、工具请求、工具结果回传、图片识别；四项全部成功才保存凭据、启用路由并同步 MaiBot 原生配置。失败保持原配置。不会重启进程或发送群消息。
- `scripts/maibot_free_api_probe.py`：支持显式地址、Bearer 密钥、模型参数和输出上限；报告不包含密钥。
- `scripts/configure_free_api_routes.py`：免费视觉后备路由优先级从 `100` 开始，避免再次挤到 DeepSeek 前面。
- 调用现有同步器更新原生配置；人设、原生回复风格、独立 embedding 模型及插件配置均保留。

## 检查结果与待办

- NVIDIA `/v1/models` 实测 HTTP 200，包含目标模型。
- 目标模型无认证聊天请求实测 HTTP 401，原因是缺少 Authorization。公开目录可访问不代表生成可匿名使用。
- 进程、用户及机器环境变量，根配置、`config/` 与插件配置均未发现 NVIDIA 密钥。
- 路由首位、禁用待验证、后备顺序、人设保留、重复执行幂等、验证后启用及密钥文件解析检查通过；脚本语法检查通过。
- 原生配置当前仍只包含两条既有可用后备聊天路由。DeepSeek 的真实认证、工具调用、图片能力及原生调用验收尚未通过，原因是缺少个人密钥；没有将未测试路由投入使用。
- 备份位于 `temp/nvidia-deepseek-backup-20261029-185123-397195/`，含私密配置，仅限本机。

密钥准备好后执行（将路径替换为实际文件，文件内容可以是原始密钥或 `NVIDIA_API_KEY=...`）：

```powershell
python scripts/configure_nvidia_deepseek.py --key-file C:\private\nvidia.env
```

成功后继续执行原生 Host/SDK API 验收，最后由用户手动重启生产 bot。当前未重启、未发送实际群测试消息。
