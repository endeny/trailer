# 热门 200 部 HF 预热（2026-11-21）

用户要求先下载热门 200 部。默认电影、英语视频；名单为当日 TMDB movie/popular 按热度顺序的前 200 个唯一 ID，固定在 `data/popular-movies-20261121.json`，不在运行中换榜。

## 运行与续传

```bash
gh workflow run warm-popular.yml --repo endeny/trailer -f language=en
```

`warm-popular.yml` 顺序执行现有 HF 下载链路，共用一次下载器/编码器安装，复用 HF 缓存，每个视频进程单独计时。单片仍尝试原生 720p/1080p，YouTube 认证不因批处理自动解除。管理员显式批处理不受 Worker 的每日 8 次自动发现额度影响；存储预算、租约和 HF-first 规则仍生效。

每部前后均将完整进度写入 R2 `trailer-control/v1/batches/popular-movies-<identity-hash>-en.json`；报告的 ready 必须对应 HF 清单，不把单独的旧 R2 视频计为 HF 成功。报告含 ready / not_found / failed / pending；无匹配预告为 not_found，不冒充成功下载。

- 重跑同一工作流会从持久检查点恢复，跳过 ready/not_found，重新处理失败与待处理项。解析器升级补充 TMDB 视频源时，旧版本的 not_found 会重新检查一次。
- 明确 Cookie 已失效时立即暂停；其余连续 3 部认证/限流失败暂停，不再尝试剩余项；HF 权限、容量、存储或租约错误立即暂停。修复 Cookie/网络/权限后再手动重跑；不自动反复重试认证问题。
- 正常运行到 315 分钟预算时暂停并自动触发下一次同工作流，避免超出 GitHub-hosted 单 job 的 6 小时限制；全流程保持单任务，未启用付费 runner。
- 每次 Actions Summary 显示统计，安全 JSON 报告 artifact 保留 3 天；持久检查点仍在 R2。不上传视频、Token、Cookie 或原始 downloader 日志作为 artifact。
- 强制取消时可能有一个在途媒体需要重新确认，现有单片租约/缓存机制负责恢复。

## 验证与追溯

基线 `6ad0a21`。新增批量与认证诊断测试覆盖名单去重、非法身份、三次认证暂停、恢复跳过、拒绝 R2 冒充 HF、时间预算检查点与无预告继续。37 项 Python + 19 项 Worker 测试通过。实际运行 ID、结果与代码 hash 在运行后补入 [VALIDATION.md](VALIDATION.md)。

来源：[TMDB Popular](https://developer.themoviedb.org/reference/movie-popular-list)、[GitHub Actions limits](https://docs.github.com/en/actions/reference/limits)。热门列表仅代表当日快照，不保证每部都有可下载预告。


## 首轮排查与恢复

- 批量代码 `991ebc1`，Cookie 轮换快速停止 `9d10c99`，TMDB 视频补充/来源版本续接 `c012339`；基线 `6ad0a21`。
- 首轮 Actions 44885000913 使用旧 Cookie，现场预检明确报告 cookies are no longer valid，Cookie 的本地过期时间尚未到不代表服务器仍接受。排查只输出错误标志，未打印 Cookie 值、原始 yt-dlp JSON 或调试日志；本次无 UI 截图。
- 用户更新本地导出后，过滤 24 条 YouTube Cookie（3665 字节）经 stdin 更新 GitHub Secret；同一源本机预检 exit 0，轮换/机器人错误标志均为 false。停止旧凭据任务，再启动 44885399820 从同一检查点恢复。
- 旧 runner 强制取消留下下载租约；确认旧 run 已 cancelled、当前唯一下载 run 尚在安装阶段后，只删除旧下载锁对象，不删除任何视频或进度对象。避免恢复时等待旧锁自然过期。
- 仓库未配置独立 TMDB_TOKEN，KinoCheck 对钢铁侠等存在预告的影片返回空，因此增加既有 tmdb.saltpi.cn 的 `/3/movie/:id/videos` 查询，沿用已有软件 attest 协议。实查 movie/1726 返回 TMDB Trailer；不改变服务端鉴权，不暴露凭据给客户端。
- 最终运行结果见 [VALIDATION.md](VALIDATION.md)，不可将 200 个任务身份与 200 个成功下载混为一谈。
