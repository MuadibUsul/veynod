# Veynod 宣传片 (50s)

`veynod-promo.mp4` — 1920×1080 · 30fps · 50 秒，中文旁白字幕 + 合成配乐。

| 时间 | 段落 | 内容 |
|---|---|---|
| 0–4.5s | 开场 | 「你有一支 AI Agent 团队」— SCOUT / ANALYST / AUDITOR / EXECUTOR 登场 |
| 4.5–15.5s | 翻车现场 | 上下文爆炸 · 互相覆盖死锁 · 生成↔审查乒乓 · 人类指令被覆盖 · 共享密钥删库 |
| 15.5–20s | 品牌亮相 | Veynod — 多 Agent 团队的共享黑板，Agent 干活，人类把关 |
| 20–41s | 六大能力 | 共享文档 · 任务看板与租约 · 熔断器 · 人类主权锁 · 实时事件总线 · 角色权限与批量撤销 |
| 41–45.5s | 架构 | 人类指挥台 / Veynod Gateway / PostgreSQL 16 + pgvector |
| 45.5–50s | 行动号召 | `docker compose up -d` · veynod.com/demo |

## 重新生成

画面是 `promo.html` 中 `render(t)` 的纯函数，音效时间点与画面事件一一对应。

```bash
python3 promo/soundtrack.py   # 生成 promo/soundtrack.wav（仅需 numpy）
node promo/render.js          # 逐帧渲染并合成 promo/veynod-promo.mp4（需 Playwright + ffmpeg）
```

- 预览：直接用浏览器打开 `promo.html`（`?t=20` 可跳到第 20 秒）。
- 抽帧检查：`node promo/render.js --stills 3,9,17`，输出到 `promo/stills/`。
- 字体从 Google Fonts 加载（Noto Sans SC、JetBrains Mono），渲染时需要联网。
