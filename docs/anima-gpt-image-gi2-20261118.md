# /anima gi2 子命令：GPT Image 远端生图（2026-11-18）

## 背景

用户要求给 `/anima` 增加 `gi2` 子命令，直接调用 uni-token 网关的 `gpt-image-2.6` 生图，
并且可以与「图生图」叠加：带参考图时走图生图（`/images/edits`），不带参考图时走文生图。

## 端点与模型

| 项 | 值 |
| --- | --- |
| 网关 | `https://api.uni-token.com/v1` |
| 模型 | `gpt-image-2.6`（网关同族还有 `-flare`、`-sunburst`、`gpt-image-2.5*`、`gpt-image-2`、`gpt-image-2-exact`、`gpt-image-1k-th`） |
| 文生图 | `POST /images/generations`（JSON） |
| 图生图 | `POST /images/edits`（multipart，字段 `image` + `model` + `prompt` + `n` + `size`） |
| 返回 | `data[0].b64_json`（约 1.5MB PNG）或 `data[0].url` |

实测（2026-11-18，各消耗 1 次额度）：文生图 ≈21s、图生图 ≈22s，均 HTTP 200；直连可用，不需要代理。

## 代码位置

| 文件 | 作用 |
| --- | --- |
| `anima/gpt_image.py` | `GPTImageClient`：`check()` 打 `/models` 探活；`generate()` 按有无参考图选端点；`_save()` 支持 b64/url、40MB 上限、按 `mime_type` 选后缀 |
| `anima/commands.py` | 选择器：`gi2` / `gptimage` / `gpt-image` → `gi2`；`gi2img` / `图生图gi2` / `gi2图生图` → `gi2_img2img`；`GPT_IMAGE_MODES` 集合 |
| `plugins/anima.py` | `execute_request()` 里 `spec.mode in GPT_IMAGE_MODES` 走远端分支，**完全跳过 ComfyUI 探活与 preflight** |

## 叠加规则

- 只写 `gi2`：文生图，直接生成，不碰 ComfyUI。
- 写 `图生图 gi2`（或 `gi2 图生图` / `gi2img`）：进入参考图等待流程，用户在 TTL 内发图片/动画表情后
  由 `auto/anima_media.py` 取第一帧 PNG 传给网关 `/images/edits`。
- 等待阶段不扣积分；`/anima 取消` 可取消。

## 积分规则（与原有链路一致）

- 只有**图片真实送达**才扣 8 积分；
- 发送未确认成功、生成异常、参考图缺失等一律不扣，并提示“本次未扣积分”；
- 生成结果落在 `AnimaService.output_cache`（临时目录），发送后由 `delete_image()` 清理。

## WebUI / config.json 字段

| 字段 | config_path | 默认 | 说明 |
| --- | --- | --- | --- |
| `gi2_api_base` | `anima.gi2_api_base` | `https://api.uni-token.com/v1` | 只填域名会自动补 `/v1` |
| `gi2_api_key` | `anima.gi2_api_key` | 空 | `secret: true`，只在 WebUI 中显示为密码框 |
| `gi2_model` | `anima.gi2_model` | `gpt-image-2.6` | |
| `gi2_size` | `anima.gi2_size` | `1024x1024` | |
| `gi2_timeout` | `anima.gi2_timeout` | `300` | 秒 |

> Key 属于敏感信息：只写在本机 `config.json`（已 gitignore），仓库里不出现任何密钥。

## 测试

`tests/test_anima_gi2.py`（22 项，全离线 mock，**不发真实生图请求**）：

- 客户端：`/v1` 补全、无 Key 拒绝、网关非 200 报错、b64 解码、`data:` 前缀、url 下载、空 data、超大图、空提示词；
- 端点选择：有参考图 → `/images/edits`，无参考图 → `/images/generations`；
- 选择器解析：6 种 `gi2` 写法；
- 插件链路：`gi2` 不走 ComfyUI、缺参考图拒绝且不扣费、参考图字节确实传给网关、`gi2_img2img` 只登记 followup 不做 ComfyUI 预检。
