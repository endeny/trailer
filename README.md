# iPlay Trailer Cache

输入 TMDB 媒体类型、ID 和视频语言，由 GitHub Actions 查找并下载对应预告片，转成 H.264/AAC MP4 后上传 Cloudflare R2。Cloudflare Worker 提供任务提交、状态查询与支持 Range 的原生播放接口。

```text
POST Worker → workflow_dispatch → TMDB/KinoCheck → yt-dlp + Deno + FFmpeg → R2
GET Worker ← R2 metadata / MP4 ← 完成后供 AVPlayer/MPV 播放
```

仅 `workflow_dispatch` 会启动下载，不会在 push、PR 或 fork 时运行下载或使用存储密钥。没有找到、需要认证或下载失败时明确返回状态，不用演示视频代替。

## 1. GitHub Secrets

在 [仓库 Actions Secrets](https://github.com/endeny/trailer/settings/secrets/actions) 配置：

| Secret | 必需 | 用途 |
| --- | --- | --- |
| `R2_ACCOUNT_ID` | 上传必需 | Cloudflare Account ID（32 位十六进制） |
| `R2_ACCESS_KEY_ID` | 上传必需 | 仅限目标 bucket 的 Object Read & Write S3 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | 上传必需 | 配套 S3 Secret Access Key |
| `TMDB_TOKEN` | 推荐 | TMDB Read Access Token；无此项时 en/de 使用 KinoCheck |
| `KINOCHECK_API_KEY` | 可选 | 提升 KinoCheck 请求额度 |
| `YOUTUBE_COOKIES` | 可选 | Netscape 格式 cookies.txt 全文，用于确实需要认证的情况 |

Actions Variable `R2_BUCKET` 默认 `media`；如果改名，须同步修改 Worker 的 `wrangler.toml`。

Cookie 不是永久凭据，也不保证消除 YouTube 的 IP/Token 验证。工作流包含 yt-dlp 的 EJS 依赖与 Deno；Actions 默认启动仅绑定 runner 回环地址的 `bgutil-ytdlp-pot-provider:2.0.2`，通过 mweb 客户端使用 PO Token。Token 仍不保证解决 IP 风控；本地验证默认不启用此服务。若 GitHub runner 下载被拒绝，应按实际错误维护下载器/认证或改用 self-hosted runner。不要将 Cookie、密钥或下载文件提交到 public 仓库，不上传原始下载日志作为 artifact。

## 2. 先验证下载环境（不需要 R2）

```bash
gh workflow run cache-trailer.yml --repo endeny/trailer \
  -f media_type=movie -f tmdb_id=299534 -f language=en -f dry_run=true
```

`dry_run` 完成真实的解析、下载、转码和媒体探测，但不连接 R2，文件在任务结束时丢弃。这可以先验证 GitHub runner 的 YouTube 可用性。非 en/de 语言需要 `TMDB_TOKEN`。

正常上传：

```bash
gh workflow run cache-trailer.yml --repo endeny/trailer \
  -f media_type=movie -f tmdb_id=299534 -f language=en
```

也可在 Actions → Cache media trailer → Run workflow 手动输入。

## 3. R2 与 Worker

需先在 Cloudflare 开通 R2（可能要求绑定付款方式），使用 Standard 存储：

```bash
npm ci
npx wrangler login
npx wrangler r2 bucket create media
```

若已有同名 bucket，直接复用，不重复创建。创建限定该 bucket 的 S3 API 密钥并填入上面的 Actions Secrets。不要把 Cloudflare 全局 API Key 当作 R2 S3 密钥。

`wrangler.toml` 已配置 `endeny/trailer`、`main` 和 `cache-trailer.yml`。Worker Secrets：

```bash
npx wrangler secret put SUBMIT_TOKEN
npx wrangler secret put GITHUB_TOKEN
npm run deploy
```

- `SUBMIT_TOKEN`：随机生成的提交凭据，只由可信后端或管理员持有，不内置进客户端。
- `GITHUB_TOKEN`：fine-grained PAT，目标为 `endeny/trailer`，Repository permissions 仅需 Actions: Read and write。这里是 Worker Secret，不是工作流自带的同名 token。
- R2 通过 binding 访问，Worker 不需要 S3 密钥。
- Worker 默认部署到 workers.dev；实际地址以 deploy 输出为准，可另配自有域名。

## 4. API

URL 中的 TMDB ID 为正整数；类型必须为 `movie` 或 `tv`，语言为两位小写语言代码。电影与剧集 ID 不共用命名空间。

```bash
# 只查询，不触发任务
curl 'https://YOUR-WORKER.workers.dev/v1/trailers/movie/299534/en'

# 提交：凭据通过本地环境变量传入，避免写在命令历史或源码中
curl -X POST \
  -H "Authorization: Bearer $TRAILER_SUBMIT_TOKEN" \
  'https://YOUR-WORKER.workers.dev/v1/trailers/movie/299534/en'
```

首次提交返回 HTTP 202；建议间隔至少 15 秒查询。状态为 `missing / pending / running / ready / failed / not_found / expired`。仅 R2 中实际存在视频对象时返回 `ready` 和播放地址：

```json
{
  "media_type": "movie",
  "tmdb_id": 299534,
  "language": "en",
  "status": "ready",
  "size_bytes": 20000000,
  "trailer_url": "https://YOUR-WORKER.workers.dev/media/movie/299534/en.mp4"
}
```

播放接口支持 GET、HEAD、单段 Range、ETag、If-None-Match 和 ETag 形式的 If-Range。状态查询与播放是公开只读接口；bucket 本身不必公开。没有跨域浏览器配置，原生客户端可直接使用。

## 5. 存储、任务与失败处理

- R2 key：`trailers/v1/{movie|tv}/{tmdb_id}/{language}/video.mp4`，同目录 `metadata.json` 记录状态与来源。
- 缓存命中时跳过解析/下载。每个媒体与语言的 Actions concurrency group 相同，运行中任务不被新提交取消。GitHub 对同组 pending run 有队列限制；本方案不保证每次重复提交都单独执行。
- Worker 的 R2 pending 状态可减少重复 dispatch，但不是原子分布式锁；同时首次提交仍可能产生多个 run，实际下载由 concurrency 串行并再次检查缓存。
- Worker pending/running 30 分钟未更新视为 expired，可重新提交；failed/not_found 15 分钟内限制重试。Actions 被强制取消、安装依赖失败或 R2 无法访问时可能无法写失败状态，由超时恢复。
- 尽量按请求语言选择官方 Trailer，再选 Teaser；最多尝试 3 个去重后的候选。不会拿其他语言、电影或演示片冒充。
- 最长 5 分钟，下载格式不超过 720p，下载单个文件上限 150 MiB；FFmpeg 输出 H.264/AAC、yuv420p、faststart，最终 MP4 上限 40 MiB。输入未知时长/直播会拒绝。
- **本版本没有总容量自动淘汰或硬配额**。单文件限制不等于总量限制。可在 R2 配置 30 天对象生命周期，或之后加入容量预算管理。不要将 Actions artifacts 当作视频仓库。
- 不自动反复重试认证错误。公开状态只记录 `youtube_auth_required`、`youtube_token_required`、`youtube_access_denied` 等固定错误码；原始下载输出不进入 public 日志。
- Public 仓库不代表无限计算资源；此工作流用于项目预告片的低频预热。大量任务、持续抓取或部署更多 runner 前，需按实际 GitHub Actions 配额和使用政策评估。

## 6. 本地验证

```bash
npm ci
npm test
npm run check
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

本地下载验证另需 `ffmpeg`、`ffprobe`、`deno` 在 PATH：

```bash
.venv/bin/python scripts/cache_trailer.py \
  --media-type movie --tmdb-id 299534 --language en --dry-run
```

## 官方参考

- [GitHub 手动/API 触发工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
- [KinoCheck API](https://api.kinocheck.com/)
- [yt-dlp EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [yt-dlp PO Token](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [R2 Workers API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)
- [R2 定价](https://developers.cloudflare.com/r2/pricing/)

## 实测记录

见 [2026-11-21 验证记录](doc/VALIDATION.md)，明确区分代码检查通过与真实下载/R2 未完成的部分。
