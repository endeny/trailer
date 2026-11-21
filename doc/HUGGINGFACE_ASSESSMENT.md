# HF 优先存储与双清晰度（2026-11-21）

最新进展：更新 YouTube Cookie 后，热门 200 部批次已成功下载并上传真实 720p/1080p。首部两档 HF CDN Range 与 ffprobe 已验证通过，见 [批量双档证据](evidence/popular-first-dual-quality-20261121.json)；下文首次接入遇到认证限制的记录为当时结果。

本次按用户要求实现 HF 为视频主存储。Actions 默认 STORAGE_BACKEND=hf；R2 保留小型任务状态、HF 提交清单、全局租约和预算计数，既有 R2 视频可回退。没有修改客户端模型或界面。

## 实现

- `scripts/download_variants.py` 原生最高 1080p 下载一次，转码 H.264/AAC/yuv420p/faststart；720p ≤40 MiB，1080p ≤120 MiB，最长 300 秒。低分辨率源不会放大冒充 1080p。
- `scripts/hf_storage.py` 向私有数据集 `saltpi/iplay-trailers` 原子提交两档 MP4 与 manifest.json。文件路径包含 SHA-256，R2 清单固定 commit SHA，缓存命中跳过下载；提交失败不发布 ready，不把新视频转存 R2。
- `scripts/hf_job.py` 串行租约下核算当前数据集文件，下载前预留 160 MiB，预算 90 GB。该预算不覆盖其他仓库或历史版本；HF 账号额度仍需监测，不保证无限免费。
- `worker/hf.mjs` 校验固定 repo、commit、路径、尺寸；HF 清单优先于旧 R2。trailer_url 为稳定 Worker 地址，默认 720p；额外 trailer_urls 可含 1080p，现有客户端继续使用原字段。
- Token 仅在 GitHub Secret/Worker Secret；Worker 请求 HF resolve 后返回短期文件签名重定向，no-store，不把主 Token 给客户端，不让视频大流量经过 Worker。HF 出错且旧 R2 文件存在才回退；无回退时 502。
- 手动 Actions 的 `refresh=true` 可重试获取更高清晰度，期间保持旧 HF 清单可读；失败保留旧快照。YouTube 不可用时可将同一身份的既有 R2 720p 视频迁入 HF，明确记录来源，不声称下载了新 1080p。
- 未配置 HF_TOKEN 时不调度新任务，不消耗自动下载预算。旧视频仍可读。

## 额度与分发

HF 免费私有存储官方当前为 100 GB（账号共享），公开存储为 Best-effort，文件解析接口有限流；本实现采用私有数据集，不把纯 App 视频缓存包装成公开训练数据集。HF 分发不等于 Cloudflare CDN HIT；原 R2 回退域名仍用原有免费 CDN，不开付费 Stream、Cache Reserve、KV、DO。

参考：[视频数据集](https://huggingface.co/docs/hub/datasets-video)、[上传指南](https://huggingface.co/docs/hub/datasets-adding)、[存储政策](https://huggingface.co/docs/hub/storage-limits)、[限流](https://huggingface.co/docs/hub/rate-limits)。

## 验证记录

基线 `6d583d2`；HF 主实现 `c739195`，域名路由 `6073743`，真实 R2 视频迁移 `41e0330`，显式质量刷新 `cda51b5`。

私有数据集与 GitHub/Worker Secrets 已配置，生产 Worker 已部署。Actions [44884182787](https://github.com/endeny/trailer/actions/runs/44884182787) 成功把电影 299534 已有真实 720p 视频迁至 HF；线上状态 `storage=hf`，即使 R2 同名视频仍存在也优先 HF。稳定地址 `https://trailers.saltpi.cn/media/movie/299534/en.mp4?quality=720p` 返回 302 / no-store / X-Trailer-Storage: hf，文件目标 `us.aws.cdn.hf.co` 返回 206 与 `bytes 0-1023/33534820`。ffprobe 实测 H.264 1280×720、AAC、185.36 秒。

YouTube 在本次 GitHub runner 与本机预检均返回认证要求。首次真实下载任务 44883998409 失败，随后迁移任务成功。1080p 转码/选择流程已有测试覆盖，但此次没有真实下载成功的 1080p 产物；不能把迁移的 720p 说成双档已实测。完整最终记录见 [VALIDATION.md](VALIDATION.md)。
