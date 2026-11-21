# 2026-11-21 链路搭建与验证记录

时间：2026-11-21（Asia/Shanghai）。

**当前验收结果：Worker 提交 → GitHub Actions → YouTube 下载/转码 → R2 media → Worker MP4 播放链路已通过真实验证；下文保留各阶段失败与修复记录。**

仓库：`endeny/trailer`。这是独立仓库，iPlay.All 和各平台子仓库未因本链路修改代码。

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

## 首次记录时的验收边界与后续步骤（现已完成）

代码已公开推送、自动检查成功；没有宣称端到端成功。后续必须依次验证：

1. GitHub runner 对真实候选的下载/转码成功（必要时配置可选 Cookie 或维护下载环境）。
2. R2 开通、配置 Secrets 后完成一次真实上传，检查 metadata 与 MP4。
3. 配置 Worker 的 SUBMIT_TOKEN / GitHub Actions write token 并部署。
4. 实测提交 → 状态转移 → 播放 URL、HEAD、Range；重复提交应命中缓存。

在以上步骤完成之前，客户端不要依赖该服务为背景预告片提供可用 URL。


## 2026-11-21：接入用户现有 media 桶

- 配置基线：`9762d83`；桶名调整代码提交：`f6af2f69cc278c9e809da59c6727f5bd7b18ac2f`。
- 用户开通 R2 并配置三个 S3 Secrets 后，`wrangler r2 bucket list` 已能看到 `media`；同步设置仓库 Variable `R2_BUCKET=media`、工作流默认值和 Worker binding。
- [真实任务](https://github.com/endeny/trailer/actions/runs/44879077614)成功向该 bucket 写入 metadata，Worker 读取得到相同的 `youtube_auth_required` 失败状态。证明 S3 凭据写入与 Worker binding 读取已连通，不等于视频上传成功。
- Worker 部署地址：`https://iplay-trailer-cache.ourfor.workers.dev`，初始部署版本 `e38c45c4-bca4-41a5-af76-65b1252de1d6`。
- `/health` 返回 200；GET 状态接口返回 200；缺少提交凭据的 POST 返回 401；尚未缓存的视频 HEAD 返回 404。默认 Python urllib User-Agent 曾遭遇 Cloudflare 边缘 1010，使用 iPlay-Trailer-Cache/1.0 正常；curl 默认 UA 也已验证正常。未修改账户安全设置。

## GitHub Cookie Secret 超限修复

- 用户反馈：粘贴 cookies.txt 后 GitHub 提示 `Value is too large`。无页面截图；为配置/API 限额问题。
- 修复前基线：`f6af2f69cc278c9e809da59c6727f5bd7b18ac2f`。
- 实际代码修复：`5fba9df4f66cfba395fedb115b6f580a0b55e01e`。
- 根因：GitHub 单 Secret 上限 48 KB；全浏览器 Cookie 导出含有其他网站的 Cookie，不应该原样配置给仅下载 YouTube 的工作流。
- 方案：增加 `scripts/prepare_cookies.py`，支持 Netscape/常见 Cookie JSON，只保留 youtube.com 及其子域，保留 HttpOnly 字段、按 domain/path/name 去重，检查字节数后通过 gh stdin 加密提交。输出仅含条数与字节数，不创建明文中间文件，不打印原始内容；不改动用户原文件。Actions 运行时再次过滤，临时文件权限 0600 并用 EXIT trap 清理。
- 实际从用户授权的本地导出中保留 22 条 Cookie、2894 字节，并成功配置仓库 Secret。未将原始导出、其他网站 Cookie 或 Cookie 值写入仓库、文档、聊天或日志。
- 新增 5 项 Cookie 测试；当前本地与 GitHub 检查合计 20 项（8 Worker + 12 Python）通过，Wrangler 打包通过。[检查记录](https://github.com/endeny/trailer/actions/runs/44879521303)。
- 提交接口随机凭据已通过 Wrangler 配置为 Worker Secret `SUBMIT_TOKEN`，本地副本在被 Git 忽略的 mode-0600 `.dev.vars`；未复用或转移 GitHub CLI 的广权限登录凭据。
- 该阶段 Worker 自动 dispatch 尚待配置 GitHub Token；后续配置与验收见下文。


## 带 Cookie 的下载客户端选择

- 修复前基线：`5fba9df4f66cfba395fedb115b6f580a0b55e01e`。
- 实际代码修复：`4c9bc6f0fa65e794b2051e010281cb618defb13d`。
- [带 Cookie 的首次真实任务](https://github.com/endeny/trailer/actions/runs/44879523849)不再报账号认证错误，但部分候选报 `youtube_token_required`，另一个候选无可用时长。
- 本地使用同一授权的 YouTube Cookie 对《奥本海默》候选 `ckvYK2QWAH4` 交叉检查：默认客户端取得 duration=130、15 个 format；强制 mweb 只返回 4 个 format，并提示 GVS PO Token 缺失；web_safari 返回 8 个 format。这表明强制 mweb 会丢失默认客户端能提供的部分格式。
- 调整为默认客户端优先，失败才使用 mweb+PO Token。不同 profile 的临时下载目录独立；新增 2 个回归测试，确保默认成功时不重复下载、默认失败才调用后备。
- 当前 8 项 Worker + 14 项 Python，共 22 项测试通过。[对应 GitHub 检查](https://github.com/endeny/trailer/actions/runs/44880203220)成功。

## 最终端到端验收（2026-11-21）

1. 用户完成 Worker GitHub 凭据配置后，使用本地提交凭据向 `/v1/trailers/movie/299534/en` 发 POST，返回 `202 pending`，确实触发了 [Actions 运行 44879978076](https://github.com/endeny/trailer/actions/runs/44879978076)。该运行成功下载、转码并上传，而不是测试替身或现有演示文件。
2. Worker GET 返回 `ready`，对象 33,534,820 字节，播放 URL：`https://iplay-trailer-cache.ourfor.workers.dev/media/movie/299534/en.mp4`。
3. 对真实远端 MP4 执行 ffprobe：H.264 1280×720，AAC，duration=185.36 秒。
4. HEAD 返回 200、`Content-Type: video/mp4`、`Content-Length: 33534820`、`Accept-Ranges: bytes`。
5. `Range: bytes=0-15` 返回 206、`Content-Range: bytes 0-15/33534820`；返回的 MP4 头含 ftyp。
6. 匹配的 If-None-Match 返回 304；未携带提交凭据的 POST 仍返回 401。
7. 对已缓存的 299534 再次提交，返回 HTTP 200 / ready，直接复用缓存，不再 dispatch。
8. 使用新客户端顺序的 [《奥本海默》任务 44880205026](https://github.com/endeny/trailer/actions/runs/44880205026)同样成功，R2 状态返回 ready。
9. 用户最初把 GITHUB_TOKEN 建为普通变量；Cloudflare 不允许同名 plain_text 直接 secret put，报 10053。在不打印凭据的情况下保留同一值、移除普通绑定后配置为加密 Secret。最终 Worker Secrets 为 GITHUB_TOKEN 与 SUBMIT_TOKEN，均未提交到 public 仓库。

后续仍需按实际使用维护 Cookie 有效期、下载器版本和缓存容量；本次没有添加全桶生命周期、删除现有 media 内容或修改 iPlay 客户端。媒体数据仅使用 trailers/v1 前缀。


## 2026-11-21 CDN 与 iPlay 后端接入

- 代码基线：3676c0b；CDN/预算修复提交：4a291aa41e57ea92743a8478920842af000db564。
- R2 自定义域名 trailers.saltpi.cn 已 active，SSL active、TLS >=1.2，盐派主域为 Free Website 计划；r2.dev 保持关闭。
- 生产 GET Range 从 MISS 变为 HIT，Age 增长，正确返回 206 与 Content-Range。奥本海默缓存头为 public,max-age=86400,s-maxage=2592000。域名直接对应 R2 对象，旧 Worker 播放 URL 重定向。
- Worker 新增鉴权的 internal/resolve 批量查询，R2 ETag 原子占位去重，每日 8 个自动任务的原子预算；默认失败 6 小时/无候选 7 天退避。
- 上传前 8 GB 全桶检查与可恢复 R2 上传租约；真实缓存元数据刷新任务 44881856895 与 44881862506 成功，验证条件写租约、全桶列举、同对象 copy 更新缓存头。仅中断分片在 trailers/v1 前缀配置 1 天清理，既有视频与默认全桶规则保留。
- 12 项 Worker +17 项 Python 测试通过，GitHub 检查 44881814407 成功。iPlay.Server 配套 8 项测试通过，生产 movie/299534 与 movie/872585 的 trailer_url 已指向 CDN，人物数据不注入字段。
- 热映页请求确实后台触发 1263337 与 1400837 的 Actions；两者仍遭遇 YouTube 验证限制，保持 null 和退避。不能把这两次失败宣称为所有影片均可下载。
- 客户端代码没有因本次接入变更。具体 iPlay.Server 代码与独立提交追溯由其 doc/issues/20261121_trailer_cdn_integration.md 维护。

## 2026-11-21：HF 优先存储已部署

- 基线 `6d583d2`；主实现 `c739195`，盐派域名路由 `6073743`，既有真实视频迁移 `41e0330`，显式清晰度刷新 `cda51b5`，刷新期间保持可播放 `62035b6`。本节为后续文档归档，不冒充功能提交。
- 私有数据集 `saltpi/iplay-trailers` 已创建；HF_TOKEN 经 stdin 配置 GitHub Secret 与 Worker Secret，无 Token/Cookie 写入 public 仓库或原始调试日志。
- Worker 生产版本 `9ec2dab0-baa2-4ea6-8392-d10518835656`，路由只接管 `trailers.saltpi.cn/media/*`；旧 R2 `trailers/v1/*` 路径继续工作。HF 优先，R2 不接收新视频，仅任务/清单/租约/计数和已有视频回退。默认 720p，源站提供时生成 1080p，可手动 refresh=true 升级。
- 19 项 Worker、26 项 Python 测试通过，Wrangler dry-run 通过；CI [44884358914](https://github.com/endeny/trailer/actions/runs/44884358914) 成功。
- 首次下载 [44883998409](https://github.com/endeny/trailer/actions/runs/44883998409) 遇 YouTube 认证失败，本机同一源预检也被拒绝。HF 存储权限与 YouTube 认证是不同环节。新增同身份旧视频迁移后，[299534](https://github.com/endeny/trailer/actions/runs/44884182787)、[872585](https://github.com/endeny/trailer/actions/runs/44884303121) 均通过真实 Actions 上传至 HF。
- 两个状态均 storage=hf，返回稳定 trailers.saltpi.cn 媒体入口，即使 R2 同名文件存在仍选择 HF；Worker 302 / no-store / X-Trailer-Storage: hf，主 Token 不下发客户端，短期文件目标为 us.aws.cdn.hf.co。
- 实际文件 Range 分别返回 206 / bytes 0-1023/33534820 与 bytes 0-1023/25573927；ffprobe 均为真实 1280×720 H.264 + AAC。生产 tmdb.saltpi.cn 两部详情均 HTTP 200 并返回对应稳定 HF URL。可检查 [去除签名与凭据的验证证据](evidence/hf-primary-20261121.json)。
- [44884472205](https://github.com/endeny/trailer/actions/runs/44884472205) 重复任务成功，日志明确 HF cache hit; download skipped，验证无重复下载。
- 1080p 编码与实际尺寸选择有自动测试覆盖，本轮源站认证被拒绝，未取得真实 1080p 产物；未把迁移的 720p 冒充双档。没有实施移动端运行测试。
- HF 当前文件预算 90 GB 不含其他仓库/历史版本，受 HF 账号实际额度和解析限流约束；HF 文件 CDN 不等同 Cloudflare HIT，不承诺无限免费存储/分发。未启用付费视频/CDN产品。


## 2026-11-21T16:37:41+08:00：热门 200 部批量下载已启动并取得双清晰度产物

- 用户授权先预下载热门 200 部。名单取 TMDB Popular 前 200 个唯一电影 ID，快照 data/popular-movies-20261121.json；默认英语。代码 `991ebc1`，Cookie 轮换诊断 `9d10c99`，TMDB 视频源补齐与恢复 `c012339`；基线 `6ad0a21`。
- 顺序单任务、逐片持久检查点、HF 缓存复用、明确 Cookie 失效立即停止/其他连续三次认证失败暂停，315 分钟运行预算后自动派发同工作流续接。显式管理员批量预热获本次 200 条授权，未修改正常 Worker 的每日 8 次自动发现限制。
- 首轮 [44885000913](https://github.com/endeny/trailer/actions/runs/44885000913) 因旧 Cookie 失效无新增视频；用户更新导出后停止旧 run。只输出认证标志，未输出 Cookie 内容；本机新凭据同源预检 exit 0，无 cookie-rotation/bot-check。GitHub Secret 更新只经 stdin。
- 清理被取消 runner 留下的旧租约时，先确认旧 run completed/cancelled、当前唯一下载 run 尚处安装阶段，仅删除旧锁对象，未删视频或批量进度。
- 当前运行：[44885399820](https://github.com/endeny/trailer/actions/runs/44885399820)。同一检查点恢复，旧解析器的 not_found 因新增 TMDB videos 查询重新检查，钢铁侠实查有官方 Trailer。
- 当前快照已完成 2 部新视频（1368337、1263337），均含原生 720p 与 1080p；批次仍在运行，不是 200 部均已完成。快照中的 11 个 failed 是旧凭据留下、尚待续跑重试的历史结果，187 个 pending 尚在队列内。持久 R2 状态优先于本归档快照：[脱敏批量报告](evidence/popular-batch-20261121.json)。
- 首部 1368337 真实 HF CDN 检查：720p H.264 1280×720 + AAC，27,459,259 字节；1080p H.264 1920×1080 + AAC，65,670,301 字节。两档稳定域名地址均返回 HF 302/no-store，文件 CDN Range 均 206，ffprobe 通过。[脱敏双档证据](evidence/popular-first-dual-quality-20261121.json)。这次已经取得真实 1080p，不再只是单测覆盖。
- 最新本地 37 项 Python + 19 项 Worker 检查通过；CI [44885397456](https://github.com/endeny/trailer/actions/runs/44885397456) 成功。批量任务不需要重新部署客户端或 Worker。


## 2026-11-21T16:45:48+08:00：TMDB-only 与电影/剧集 200+200 并行任务

- 用户要求直接使用 TMDB，并增加热门前 200 部电视剧、并行提交。基线 `20c4192`，代码 `48ddd41`。
- TMDB 一直是候选优先来源；本次关闭默认 KinoCheck 补充请求，生产工作流仅使用 TMDB。独立 Token 缺省时经既有 tmdb.saltpi.cn 调用 videos；未更改客户端或代理鉴权。
- 已生成 `data/popular-tv-20261121.json`：TMDB `/3/tv/popular` 前 200 个唯一 TV ID。movie/tv 名单、状态路径、检查点、concurrency group 分离，自动续跑保留 media_type。
- 原全局锁覆盖下载/编码，两个 runner 无法真正并行。改为身份锁覆盖单媒体，全局锁只覆盖容量检查与 HF commit，保证并行下载但提交预算仍串行。双线程测试要求两个类型同时进入下载，并检查上传持有全局锁。
- 42 项 Python +19 项 Worker 测试通过；CI [44886264846](https://github.com/endeny/trailer/actions/runs/44886264846) 成功，工作流 YAML 解析通过。
- 为应用并行锁升级，停止旧电影 run 44885399820，确认 completed/cancelled 且没有其他下载任务后，清理其遗留全局锁；保留所有视频与批量检查点。
- 电影续跑 [44886290089](https://github.com/endeny/trailer/actions/runs/44886290089)，电视剧新增 [44886294451](https://github.com/endeny/trailer/actions/runs/44886294451)。已观察到两条 run/job 同时 in_progress（开始时间分别 08:44:40Z、08:44:43Z），不是相同 concurrency group 下排队。
- 当前为提交并运行中的 200+200 队列，不能表述为 400 个预告已下载完成。原已完成 HF 视频继续命中缓存；720p/1080p 与 HF 优先规则不变。
