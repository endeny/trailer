# 2026-11-21 链路搭建与验证记录

时间：2026-11-21（Asia/Shanghai）。仓库：`endeny/trailer`。这是独立仓库，iPlay.All 和各平台子仓库未因本链路修改代码。

## 提交追溯

- 初始实现/复现基线：`10d6ac3`，包含 Actions、Worker、下载脚本、R2 状态协议和测试。
- KinoCheck 查询修复：`62f9d9c1fbc76e7f0677bcfe79143269adf64e0b`。
- PO Token 服务与错误分类：`58b3b203c3335fe543b37c744649a2469b4f0ed6`。
- 本文为后续归档，不是代码修复提交。

## 已通过的检查

- `npm test`：8 项 Worker 测试，覆盖鉴权、dispatch、任务状态、缓存存在性及 GET/HEAD/Range/ETag。
- `python -m unittest discover -s tests -p 'test_*.py'`：7 项测试，覆盖参数校验、候选匹配、缓存去重、无预告、失败状态、上传完成后才发布 ready。
- `npm run check`：Wrangler dry-run 打包成功；不等于远端部署成功。
- [GitHub 上的检查](https://github.com/endeny/trailer/actions/runs/44878622491)：成功。

## KinoCheck 分类过滤回归

第一次真实 Actions dry-run（[运行记录](https://github.com/endeny/trailer/actions/runs/44878179812)）未取得候选。

排查发现：`/movies?tmdb_id=299534&language=en&categories=Trailer,Teaser` 返回 HTTP 200，但 body 为业务错误 `One or more categories not found`。移除 categories 参数后，同一媒体返回正常电影结构及候选预告片。不能只凭 HTTP 200 判断请求成功。

修复方案：

1. 向 KinoCheck 请求完整候选列表，在本地按视频语言和 Trailer/Teaser 分类过滤。
2. `request_json` 检查 JSON 类型和 error 字段，业务错误归类为 `source_api_failed`。
3. 顶层 JobError 输出固定错误码，不再全部显示泛化的初始化失败。
4. 增加该查询方式的回归测试。错误输出不包含 Cookie、密钥、完整媒体流 URL 或原始下载日志。

这是 API/命令行排查，没有页面截图。

## YouTube 下载实测

- 使用电影 TMDB ID `299534`，语言 `en`，由 KinoCheck 取得候选 ID `O1M_iqSBEpA`、`YCS1f17E2Lo`、`x9QC8hvXSSs`。
- 本机匿名 yt-dlp 对首个候选返回 `Sign in to confirm you’re not a bot`；这证明本机该次请求遭遇认证检查，不代表所有环境/视频都必须登录。
- [GitHub 匿名下载](https://github.com/endeny/trailer/actions/runs/44878394848)失败，未产生 MP4。
- 启用官方指南推荐的 PO Token provider 后，[GitHub 再次实测](https://github.com/endeny/trailer/actions/runs/44878624342)：provider 成功启动，3 个候选仍返回 `youtube_video_unavailable`，下载未通过。不能据此确认视频已删除，也不能声称 Cookie 必定解决。
- 换用电影 TMDB ID `872585`（《奥本海默》）交叉验证：[运行记录](https://github.com/endeny/trailer/actions/runs/44878805832)。3 个候选均返回 `youtube_auth_required`。该 runner 的这次测试需要认证；没有证明 Cookie 配好后一定能下载。
- PO Token 服务仅绑定 runner 的 127.0.0.1，结束后删除容器。没有配置/使用个人 Cookie。

## R2 状态

Cloudflare 已有登录态，但首次列举 bucket 返回 `10001`；再次请求明确返回 `10042: Please enable R2 through the Cloudflare Dashboard`。因此没有创建 bucket、部署绑定 R2 的 Worker、上传视频或验收真实播放 URL。

用户需要在 Cloudflare 控制台先开通 R2，创建默认位置、Standard 存储的 `iplay-trailers` bucket；生成只授权该 bucket Object Read & Write 的 S3 凭据，写入 GitHub Actions Secrets：`R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`。Secret Access Key 仅创建时显示一次，不通过聊天传递。

## 验收边界与后续步骤

代码已公开推送、自动检查成功；没有宣称端到端成功。后续必须依次验证：

1. GitHub runner 对真实候选的下载/转码成功（必要时配置可选 Cookie 或维护下载环境）。
2. R2 开通、配置 Secrets 后完成一次真实上传，检查 metadata 与 MP4。
3. 配置 Worker 的 SUBMIT_TOKEN / GitHub Actions write token 并部署。
4. 实测提交 → 状态转移 → 播放 URL、HEAD、Range；重复提交应命中缓存。

在以上步骤完成之前，客户端不要依赖该服务为背景预告片提供可用 URL。
