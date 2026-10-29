# Veynod 宣传片

| 文件 | 规格 | 主题 |
|---|---|---|
| `veynod-promo.mp4` | 16:9 · 1920×1080 · 50 秒 | 翻车现场 → 六大能力 → 架构 |
| `veynod-promo-vertical.mp4` | 9:16 · 1080×1920 · 58 秒 | 一个 Agent → 一个团队 → 一张图 → 一个 Loop → 一张网 |

两支都是 30fps、中文字幕 + 合成配乐。

## 竖屏短视频（9:16）

| 时间 | 段落 | 内容 |
|---|---|---|
| 0–3.2s | 钩子 | 1 个 Agent → 10 个 Agent，「一起干活会怎样？」 |
| 3.2–7.2s | 翻车 | 互相覆盖 · 死锁 · 乒乓循环 · 人类指令被覆盖 · 删库 |
| 7.2–11s | 品牌 | Veynod — Agent 干活，人类把关 |
| 11–17s | 地基 | 已上线的六大能力（共享黑板、任务租约、熔断、主权锁、实时事件、批量撤销） |
| 17–29s | 第一层 · 图 | 一句话需求 → 依赖图 → 人类确认并冻结 → 每个 chunk 派给一个 Agent → 熔断换人 → 下游自动解锁 → 全图合并 |
| 29–41s | 第二层 · Loop | 全自动模式、7×24 流水线、任意电脑作为 runner 加入；技术问题按安全默认值自决，只有钱/身份/法务/删数据/对外承诺找人；90 秒内发现停摆 |
| 41–53s | 第三层 · 联邦（建设中） | 工作区互联、方向性授权、签名信封委派、成本收据不借密钥 → 跨组织 Agent 协作网络 |
| 53–58s | 行动号召 | veynod.com/demo · `docker compose up -d` |

联邦部分依据 `docs/federation/` 中已定的架构决策，片中标注为「建设中」。

## 横屏（16:9）

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
python3 promo/soundtrack.py            # promo/soundtrack.wav（仅需 numpy）
node promo/render.js                   # promo/veynod-promo.mp4（需 Playwright + ffmpeg）
python3 promo/soundtrack_vertical.py   # promo/soundtrack_vertical.wav
node promo/render.js vertical          # promo/veynod-promo-vertical.mp4
```

- 预览：直接用浏览器打开 `promo.html` 或 `promo-vertical.html`（`?t=20` 可跳到第 20 秒）。
- 抽帧检查：`node promo/render.js vertical --stills 3,9,17`，输出到 `promo/stills/vertical/`。
- 两支配乐共用 `synth.py` 里的乐器和音效。
- 字体从 Google Fonts 加载（Noto Sans SC、JetBrains Mono），渲染时需要联网。
