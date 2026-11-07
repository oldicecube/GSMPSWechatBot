# 对称插件的表情包与 GIF 支持

用法保持 `/对称 左右`、`/对称 右左`、`/对称 上下`、`/对称 下上`。在等待时间内，同一用户的下一条图片或表情包被处理一次；其他用户不消费状态。下一条普通文字会取消本人的等待。WebUI「GSMPS Wechatbot → picture」继续提供等待时间设置，默认 60 秒。

## 链路修改

- `auto/picture.py` 识别图片、表情包标志和微信 type 47；按原消息读取二进制，交给 `utils/symmetry_image.py`，发送结束清理唯一命名的临时文件。不会消费其他插件拥有的等待状态。
- `core/weflow_media.py` 复用 WeFlow 精确原消息接口，按会话及 localId/serverId 获取图片或表情，拒绝返回 ID 不一致。取消对称路径上的「最近图片 / 最近时间」回退，不借用别人发来的图片。有限流式下载及 20 MB 上限，外部文件地址不携带 WeFlow Token。
- QQ 继续使用适配器传入的图片 / EmojiComponent 原字节；同时保留作为 FileComponent 上传的 GIF/PNG/JPEG/WebP/BMP 图片文件。缺少原始内容时不调用微信接口。
- WeFlow 的表情导出改用现有 `downloadEmojiFile`，补齐只有本地缓存、或通过 MD5 找到缓存且没有 CDN URL 的转发表情，返回原始 GIF 字节。
- 静态图片输出 PNG；动画逐帧合成后对称，输出 GIF，保留帧时长、循环次数和不循环语义。使用独立透明索引与清除背景策略，避免透明区域变黑及前帧拖影；不把不透明黑色当透明。PNG 的半透明 alpha 原样保留，GIF 透明度按格式限制为透明 / 不透明。
- 四向处理修复奇数尺寸的中线偏移；复制像素不再重复乘 alpha。限制输入及输出 20 MB、单帧 1600 万像素、最多 300 帧、总解码 4000 万像素，超限拒绝，不静默截成首帧。
- QQ 输出 GIF 走已有原生 EmojiComponent，微信沿用文件发送出口，传入 GIF 原文件；实际微信客户端的呈现仍取决于既有文件发送能力。

## 验证与生效

`scripts/symmetry_animation_acceptance.py` 验证四向像素、中线、PNG 半透明、GIF disposal 1/2/3 和局部帧、透明背景与不透明黑色、时长与循环、大小 / 帧数限制、双端下一条表情包流程、同用户隔离、精确取图、文件清理、QQ 原生 GIF 输出及下载限制。

`node scripts/symmetry_weflow_acceptance.cjs` 执行源码中实际 TypeScript 方法，验证缓存 / MD5 / CDN 三条解析路径、原 GIF 导出、缺失文件及表情导出关闭分支。WeFlow esbuild 构建通过。原双端壳子插件 138/138 检查通过；兼容性检查同时修正仅 @ 策略对缺少 chat_stream 的旧测试 / 兼容对象的读取方式。

不发送真实群消息，不重启 bot 或 WeFlow。源码与 WeFlow 本地构建已准备好，由用户下次启动 / 重启完整应用。
