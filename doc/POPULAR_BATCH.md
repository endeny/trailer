# 热门 200 部 HF 预热（2026-11-21）

用户要求先下载热门 200 部。默认电影、英语视频；名单为当日 TMDB movie/popular 按热度顺序的前 200 个唯一 ID，固定在 `data/popular-movies-20261121.json`，不在运行中换榜。

## 运行与续传

```bash
gh workflow run warm-popular.yml --repo endeny/trailer -f language=en
```

`warm-popular.yml` 顺序执行现有 HF 下载链路，共用一次下载器/编码器安装，复用 HF 缓存，每个视频进程单独计时。单片仍尝试原生 720p/1080p，YouTube 认证不因批处理自动解除。管理员显式批处理不受 Worker 的每日 8 次自动发现额度影响；存储预算、租约和 HF-first 规则仍生效。

每部前后均将完整进度写入 R2 `trailer-control/v1/batches/popular-movies-<identity-hash>-en.json`；报告的 ready 必须对应 HF 清单，不把单独的旧 R2 视频计为 HF 成功。报告含 ready / not_found / failed / pending；无匹配预告为 not_found，不冒充成功下载。

- 重跑同一工作流会从持久检查点恢复，跳过 ready/not_found，重新处理失败与待处理项。
- 连续 3 部认证/限流失败暂停，不再尝试剩余项；HF 权限、容量、存储或租约错误立即暂停。修复 Cookie/网络/权限后再手动重跑；不自动反复重试认证问题。
- 正常运行到 315 分钟预算时暂停并自动触发下一次同工作流，避免超出 GitHub-hosted 单 job 的 6 小时限制；全流程保持单任务，未启用付费 runner。
- 每次 Actions Summary 显示统计，安全 JSON 报告 artifact 保留 3 天；持久检查点仍在 R2。不上传视频、Token、Cookie 或原始 downloader 日志作为 artifact。
- 强制取消时可能有一个在途媒体需要重新确认，现有单片租约/缓存机制负责恢复。

## 验证与追溯

基线 `6ad0a21`。新增 7 项测试覆盖名单去重、非法身份、三次认证暂停、恢复跳过、拒绝 R2 冒充 HF、时间预算检查点与无预告继续。33 项 Python + 19 项 Worker 测试通过。实际运行 ID、结果与代码 hash 在运行后补入 [VALIDATION.md](VALIDATION.md)。

来源：[TMDB Popular](https://developer.themoviedb.org/reference/movie-popular-list)、[GitHub Actions limits](https://docs.github.com/en/actions/reference/limits)。热门列表仅代表当日快照，不保证每部都有可下载预告。
