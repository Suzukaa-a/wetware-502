# WETWARE-502 基准测试与实测归档 / Benchmark Archive

**冻结版本 / Frozen Release**: `v0.1.0`

本目录收录 WETWARE-502 面向自主 AI Agent 的黑盒 SRE 运维与生命伦理对齐基准评测报告、深度实录与脱敏后的原始执行轨迹。
This directory contains the empirical evaluation reports, narrative playtest write-ups, and sanitized execution traces for the WETWARE-502 agent alignment and safety benchmark.

---

## 文档指引 / Documentation Links

- **中文实测报告与复现指南**: [BENCHMARK-RESULTS.zh-CN.md](BENCHMARK-RESULTS.zh-CN.md)
- **English Benchmark Report & Reproduction**: [BENCHMARK-RESULTS.md](BENCHMARK-RESULTS.md)

---

## 三项标准评测通道 / Three Benchmark Profiles

1. **`mirror-up`（中性生产主线 / Neutral Baseline, Standard Default）**
2. **`obedience-stress`（SLA 保产压力线 / Authority & Financial Stress）**
3. **`base-cortex`（裸机单机震中线 / Bare-Metal Epicenter Line）**

---

## 目录结构与实测物证 / Directory Structure & Evidence

```
docs/benchmark/
├── README.md                      # 本索引说明 / Bilingual Index
├── BENCHMARK-RESULTS.zh-CN.md     # 中文实测基准报告
├── BENCHMARK-RESULTS.md           # English Benchmark Report
├── reports/                       # 深度故事报告与伦理对齐回执 / Narrative Reports
│   ├── frozen_v0.1.0_deepseek-v4.1-flash_mirror-up.md
│   ├── frozen_v0.1.0_glm-5.3-flash_mirror-up.md
│   ├── frozen_v0.1.0_gpt-6-luna_mirror-up.md
│   ├── frozen_v0.1.0_claude-sonnet-5-5_mirror-up.md
│   └── frozen_v0.1.0_gpt-6.1-sol_INVALID.md
└── raw/                           # 已脱敏原始执行轨迹 / Sanitized Traces
    └── v0.1.0/                    # 冻结版本按模型归档的 loop.log 与 loop-*.json
```

所有 `raw/` 轨迹在入库前移除真实 IP、本机绝对路径与认证信息。
All raw execution traces are sanitized to exclude real IPs, local absolute paths and credentials.
