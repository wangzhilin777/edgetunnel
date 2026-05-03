# region-probe-docker

给 `edgetunnel` 的 `REGION_PROBE_API` 用的外部地区探测服务。

它的作用不是查 IP 库归属地，而是直接在 Docker 所在机器上，对目标 `host:port` 发起实际连通测试，请求：

- `https://speed.cloudflare.com/cdn-cgi/trace`
- 或明文端口时直接请求 `http://speed.cloudflare.com/cdn-cgi/trace`

然后优先读取返回内容里的 `colo=XXX`，再映射成国家简称；如果拿不到 `colo`，就回退读取响应头里的 `CF-RAY` 机房码再映射地区。

如果这两种信息都拿不到，`edgetunnel` 订阅侧会把该节点标记成 `OFF-节点名`，表示本次探测未拿到有效地区结果。

这样比纯 IP 库更接近“本次实际命中的 Cloudflare 接入地区”。

## 目录

- `server.py`：主程序
- `Dockerfile`：镜像构建文件
- `docker-compose.yml`：直接启动用
- `.env.example`：安装时先复制成 `.env`

## 接口

### 健康检查

`GET /healthz`

### 地区探测

`POST /region-probe`

请求：

```json
{
  "targets": [
    {
      "host": "172.67.78.188",
      "port": 443,
      "remark": "CF官方优选1"
    },
    {
      "host": "8.39.125.116",
      "port": 2096,
      "remark": "CF官方优选2"
    }
  ]
}
```

返回：

```json
{
  "success": true,
  "service": "region-probe-docker",
  "success_count": 2,
  "total": 2,
  "data": [
    {
      "host": "172.67.78.188",
      "port": 443,
      "remark": "CF官方优选1",
      "colo": "NRT",
      "region": "JP",
      "ip": "172.67.78.188",
      "source": "trace",
      "latency_ms": 418,
      "error": null
    }
  ],
  "map": {
    "172.67.78.188:443": "JP",
    "8.39.125.116:2096": "SG"
  }
}
```

## 直接部署

### 方式 0：直接运行 Python

这个服务不依赖第三方库，只有 Python 3 就能直接运行，不一定非要 Docker。

进入目录后直接启动：

```bash
cd region-probe-docker
python server.py
```

默认监听：

- `0.0.0.0:8080`

如果要临时指定端口和 token，例如：

PowerShell：

```powershell
$env:LISTEN_PORT="8226"
$env:REGION_PROBE_TOKEN="你的token"
python server.py
```

如果要走代理，例如：

```powershell
$env:PROXY_URL="socks5://127.0.0.1:7891"
python server.py
```

### 方式 1：docker compose

```bash
cd region-probe-docker
cp .env.example .env
# 先修改 .env 里的 token / 代理
docker compose pull
docker compose up -d
```

如果安装人员不想折腾 `.env`，也可以直接用下面这个“固定版” `docker-compose.yml`：

```yaml
services:
  region-probe:
    image: ghcr.io/wangzhilin777/region-probe-docker:latest
    container_name: region-probe
    ports:
      - "8226:8080"
    environment:
      LISTEN_HOST: 0.0.0.0
      LISTEN_PORT: 8080
      TRACE_HOST: speed.cloudflare.com
      TRACE_PATH: /cdn-cgi/trace
      TLS_SERVER_NAME: speed.cloudflare.com
      CONNECT_TIMEOUT: 3.5
      READ_TIMEOUT: 4.5
      MAX_WORKERS: 24
      MAX_TARGETS: 500
      REGION_PROBE_TOKEN: "改成你自己的token"
      PROXY_URL: ""
      PROXY_REMOTE_DNS: "false"
    restart: unless-stopped
```

如果要走本地代理，把 `PROXY_URL` 改成例如：

```yaml
PROXY_URL: "socks5://host.docker.internal:7891"
```

或者：

```yaml
PROXY_URL: "http://host.docker.internal:7890"
```

默认 `docker-compose.yml` 已经直接指向：

- `ghcr.io/wangzhilin777/region-probe-docker:latest`

安装人员更推荐直接改 `.env`，至少改这几个：

- `REGION_PROBE_TOKEN`
- `HOST_PORT`
- `PROXY_URL`：如果要走本地 Clash / Verge / socks5，就填这个

### 方式 2：docker build

```bash
cd region-probe-docker
docker build -t region-probe-docker .
docker run -d --name region-probe -p 8226:8080 region-probe-docker
```

### 方式 3：GitHub 自动构建镜像

仓库已经带了 GitHub Actions 工作流：

- `.github/workflows/region-probe-image.yml`

当你把 `region-probe-docker` 相关改动推到 `main` 后，会自动构建并推送到：

- `ghcr.io/你的 GitHub 用户名/region-probe-docker:latest`

服务器可直接拉取：

```bash
docker pull ghcr.io/你的 GitHub 用户名/region-probe-docker:latest
docker run -d --name region-probe -p 8226:8080 ghcr.io/你的 GitHub 用户名/region-probe-docker:latest
```

## 可选环境变量

- `REGION_PROBE_TOKEN`
  - 设置后，请求时要带：
  - `Authorization: Bearer 你的token`
  - 或 `X-Region-Probe-Token: 你的token`
- `CONNECT_TIMEOUT`
  - 连接超时，默认 `3.5`
- `READ_TIMEOUT`
  - 读取超时，默认 `4.5`
- `MAX_WORKERS`
  - 并发线程数，默认 `24`
- `MAX_TARGETS`
  - 单次最多探测数量，默认 `500`
- `TRACE_HOST`
  - 默认 `speed.cloudflare.com`
- `TRACE_PATH`
  - 默认 `/cdn-cgi/trace`
- `TLS_SERVER_NAME`
  - 默认 `speed.cloudflare.com`
- `PROXY_URL`
  - 不走代理就留空
  - 支持：
  - `socks5://host.docker.internal:7891`
  - `socks5h://host.docker.internal:7891`
  - `http://host.docker.internal:7890`
- `PROXY_REMOTE_DNS`
  - 默认 `false`
  - 当你想让代理端解析域名时再开

## 在 edgetunnel 里怎么配

你在 Pages / Worker 环境变量里这样配：

- `REGION_PROBE_API=https://你的服务器域名/region-probe`
- `REGION_PROBE_TOKEN=你的token`

如果你用了 `docker-compose.yml` 默认端口，反代到公网后地址通常类似：

- `https://probe.example.com/region-probe`

默认本地端口映射为：

- `8226 -> 8080`

如果想让探测结果尽量贴近“用户实际出口地区”，推荐把探测服务和用户实际使用时走同一个代理出口。比如：

- Clash Verge 的 `mixed port` / `http` 代理
- `socks-port`

例如：

```env
PROXY_URL=socks5://host.docker.internal:7891
```

或者：

```env
PROXY_URL=http://host.docker.internal:7890
```

## 注意

- 这个程序运行在你自己的服务器上，所以不会受到 Cloudflare Workers 那种子请求和 socket 限制。
- 它优先按 `colo` 映射国家简称，所以结果更接近“这次实际命中的 Cloudflare 接入机房/地区”。
- Cloudflare Anycast 本身就可能波动，所以同一个 IP 在不同时间、不同网络下，命中的 `colo` 也可能变化。
