# Hugging Face 预告片存储可行性（2026-11-21）

结论：技术上可以存 MP4 与 720p/1080p 两档，也可以保留现有 trailer_url 契约，仅修改 Actions/Worker 后端。但尚未配置 HF 数据集或执行上传，当前生产仍使用 R2 与真实 CDN。

官方视频数据集支持直接存放 MP4，并通过 metadata.jsonl 等记录视频信息：[视频数据集](https://huggingface.co/docs/hub/datasets-video)、[上传指南](https://huggingface.co/docs/hub/datasets-adding)。

免费用户/组织当前私有存储为 100 GB，公开存储为 Best-effort。公开大数据集应具备社区复用价值，不能据此承诺“无限免费网盘/CDN”。该评估不把纯 App 缓存伪装成机器学习数据集：[存储政策](https://huggingface.co/docs/hub/storage-limits)。

文件 resolve 接口也有限流，超过返回 429；限额按五分钟窗口计算，Free/匿名额度可能调整。因此只读文档不能替代实际视频 Range、重定向、缓存及部署网络测试：[限流](https://huggingface.co/docs/hub/rate-limits)。

建议的后端方案（尚未实现）：

- Actions 仅在源站确实提供 1080p 时下载最高 1080p，制作 720p 与 1080p H.264/AAC MP4，记录尺寸、时长、字节数和源视频 ID；不把 720p 放大冒充 1080p。
- 路径示例 movie/<tmdb_id>/<language>/<youtube_id>/720p.mp4 和 1080p.mp4，一次提交两档及元数据，减少提交数量；不把 Hub Git 仓库当作高频状态数据库。
- HF 承担归档，R2 保留受限的热门播放缓存；当前 trailers.saltpi.cn 继续负责 CDN。这个组合是工程建议，HF 的免费可用性与是否符合托管政策仍由平台决定。
- Worker 从元数据选择版本填入原 trailer_url，默认保持 720p；客户端无需知道 HF Token、上传及存储细节。让用户在界面手动选择清晰度才需要另做 UI 能力，当前没有实现该能力。
- 私有数据集 Token 留在后端，不能拼到公开播放 URL。永久 resolve 路径与其临时重定向目标应区别处理，不把临时签名地址长期写死。

接入所需：用户指定数据集 ID（owner/name）、可见性与受限 HF 写入 Token；完成实际上传和播放测试后才切换生产。当前 1080p/HF 都属于待接入方案，现有已部署下载上限仍为 720p。
