# 09 — Local-KB: 零知识本地知识库（复用 1Password 加密模块思维）

## 定位
Notion / 语雀 类的隐私切片：零知识（E2EE）团队知识库，用户数据加密后才落盘/同步，
服务端不可读。复用 01-passvault 的加密模块（AES-256-GCM + 密钥派生）。
母市场：Notion（增值）百亿级。

## 主要痛点
- 团队笔记/KB 存在 Notion/语雀，内容等于给平台脱敏后阅读
- 合规（医疗器械/法务/财务）要求 data 加密存储
- 需要本地优先 + 可选托管同步

## 功能需求
1. 文档模型：类似 Notion block 的结构（title/paragraph/heading/code/quote/table）
2. 零知识加密：
   - 工作区密钥从主密钥派生（PBKDF2/Argon2id）
   - 每个文档用随机 per-doc DEK 加密，DEK 用工作区密钥包裹
   - 本地 SQLite 存储密文；备份 = 表单密文的 `.kb` 文件（可导入）
3. 协作者：加密分享（X25519 + 共享密钥），可选（第 2 期）
4. 本地 Markdown 渲染 + 编辑器（textarea + markdown parse，或简单块编辑）
5. 搜索：本地 SQLite FTS5（明文在内存中索引，遵守「零知识：磁盘上不出」）
6. 同步（可选，v2）：单纯密文同步平面（操作日志不含明文）

## 非目标
- 实时协同（OT/CRDT 多人编辑，第 2 期单写者即可）
- 全 workspace 复杂权限模型（v1: 单写者 + 只读分享）

## 技术栈
- Node v22 或 Python 3.14 单页应用（可以纯 client：去服务器模式，local-first）
- 加密：WebCrypto / libsodium.js
- 存储：IndexedDB（web）/ SQLite（desktop）；PDF export 用 07 组件启发
- Markdown：marked / markdown-it

## 低算力守则
见全局（本地加密 + FTS + 无 GPU 完全合规）。

## AC
1. 创建/编辑/删除文档，关闭浏览器重开数据仍在（持久化）
2. 加密验证：磁盘/IndexedDB 中无明文（grep 词标题内容验证为密文）
3. 无主密码（或错误密码）无法打开文档
4. .kb 导出/导入可换机器恢复
5. 搜索 FTS 命中（本地索引）
6. 纯本地离线可用（无服务器 demo 页面）
7. 测试 + README（加密架构文档）

## Phases
- P0: 数据模型 + 加密层 + 本地存储
- P1: 编辑器 + 渲染 + 搜索
- P2: .kb 备份 / 导入导出
- P3: Web 同步平面（optional）+ 协作（optional）

## 里程碑
M1: 加密 + 编辑 + 存储（MVP）
M2: 搜索 + 备份
M3: 同步/协作

## 变现
- 开源 core，托管同步 = 付费项（E2EE 不读内容）
