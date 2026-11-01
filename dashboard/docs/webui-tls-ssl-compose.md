# MaiBot WebUI Compose TLS/SSL 教程

本文档专门说明 Docker Compose 部署下如何通过 Caddy 为 MaiBot WebUI 提供 HTTPS。

## 1. 目标结构

启用后，网络结构应为：

```text
浏览器
  -> https://maibot.example.com
  -> Caddy 容器 :80/:443
  -> core 容器 :8001
  -> MaiBot WebUI
```

这意味着：

1. core 不再直接对公网暴露 8001
2. Caddy 统一接管 80 和 443
3. Caddy 通过 Docker 网络访问 core:8001

## 2. 仓库里已经补了什么

本仓库已补充以下内容：

1. 根目录 docker-compose.yml 中新增了默认注释的 Caddy 示例块
2. 根目录 docker-compose.yml 中新增了默认注释的 Caddy 数据卷定义
3. dashboard/docs/Caddyfile.docker.example 提供了 Docker Compose 专用配置模板
4. dashboard/docs/Caddyfile.host.example 提供了非 Docker 宿主机专用配置模板

## 3. 需要手动注释或启用的段落

本文档按默认保持注释状态进行说明，下面明确列出需要操作的段落。

### 3.1 需要注释掉的现有段落

启用 Caddy 以后，请注释掉根目录 docker-compose.yml 中 core 服务下这一段端口映射：

```yaml
ports:
  - "18001:8001"
```

原因很简单：

1. 这段会把 WebUI 的明文 HTTP 直接暴露到宿主机
2. 启用 HTTPS 以后，应由 Caddy 对外暴露 80 和 443
3. 避免出现“HTTPS 入口和 HTTP 入口同时暴露”的混乱状态

### 3.2 需要取消注释并启用的段落

启用时，需要在根目录 docker-compose.yml 中取消注释这两部分：

1. caddy 服务块
2. volumes 里的 caddy_data 和 caddy_config

## 4. 启用前需要准备什么

1. 域名已经解析到服务器公网 IP
2. 宿主机的 80 和 443 未被占用
3. 防火墙和云安全组已放行 80 和 443
4. WebUI 当前可以通过 compose 正常启动
5. 已准备修改 dashboard/docs/Caddyfile.docker.example 里的域名

## 5. Caddy 配置文件如何写

Docker Compose 模式请使用：dashboard/docs/Caddyfile.docker.example

非 Docker 宿主机模式请使用：dashboard/docs/Caddyfile.host.example

最小可用配置如下：

```caddyfile
maibot.example.com {
    reverse_proxy core:8001
}
```

建议至少做这两处修改：

1. 把 maibot.example.com 改成实际使用的域名
2. 如果有额外安全要求，再按需增加 header 配置

## 6. compose 启用步骤

### 6.1 修改 WebUI 配置

先在 docker-config/mmc/bot_config.toml 中确认：

```toml
[webui]
host = ["0.0.0.0"]
mode = "production"
secure_cookie = true
trust_xff = true
```

trusted_proxies 的建议值取决于实际网络环境。

如果 Caddy 和 core 在同一个 Docker 网络里，建议先按实际来源地址或网段填写。不要为了省事直接把范围开得过大。

### 6.2 修改 Caddyfile

编辑 dashboard/docs/Caddyfile.docker.example，将域名替换为真实值。

### 6.3 修改 compose

1. 注释掉 core 服务里对外暴露 WebUI 的 ports 段
2. 取消注释 caddy 服务块
3. 取消注释底部 volumes 里的 caddy_data 和 caddy_config

### 6.4 启动服务

```bash
docker compose up -d
```

### 6.5 查看日志

```bash
docker compose logs -f caddy
docker compose logs -f core
```

## 7. Let's Encrypt 申请与续期

### 7.1 证书申请触发条件

Caddy 容器启动后，满足以下条件时会自动申请证书：

1. 域名已解析到当前服务器
2. 80 和 443 对公网开放
3. Caddy 能成功接收到针对该域名的请求

### 7.2 自动续期说明

Caddy 会自动续期，通常不需要编写 crontab，也不需要手工执行 certbot。

只需要确保：

1. caddy_data 卷被持久化
2. 容器会长期运行
3. 域名长期指向同一台服务器或新服务器已同步迁移数据
4. 80 和 443 没被防火墙阻断

### 7.3 续期检查建议

建议定期执行：

```bash
docker compose logs --tail=200 caddy
docker compose ps
```

重点关注：

1. ACME 申请失败
2. 证书续期失败
3. 端口绑定失败
4. 域名解析不一致

## 8. 常见错误与排查

### 8.1 证书申请失败

优先检查：

1. 域名是否指向服务器公网 IP
2. 是否已经开启 CDN 代理但未正确放通验证流量
3. 80 和 443 是否被云厂商安全组拦截
4. 宿主机是否还有别的程序占用了 80 或 443

### 8.2 登录失败

优先检查：

1. webui.secure_cookie 是否已启用
2. 请求是否真正走 https:// 域名
3. 代理是否正确传递了 X-Forwarded-Proto

### 8.3 WebSocket 连接失败

优先检查：

1. Caddy 是否已正确反向代理到 core:8001
2. 页面是否通过 HTTPS 打开
3. 浏览器开发者工具里是否出现混合内容报错

## 9. 迁移建议

如果当前已经在使用：

```yaml
ports:
  - "18001:8001"
```

那说明当前还是“宿主机明文 HTTP 暴露 WebUI”模式。迁移到 HTTPS 时建议：

1. 先准备好域名
2. 先改好 Caddyfile
3. 再切换 compose 暴露方式
4. 切换后直接以 https://域名 访问，不再继续使用 http://服务器IP:18001
