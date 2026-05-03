# GRIS — 全球法规情报系统

一个自动追踪全球电动出行产品(电动滑板车、电动自行车、电动摩托车、智能割草机等)
最新法规动态的工具。

它会自动:

1. **发现** — 用 Gemini AI 搜索全球各国监管机构的最新法规
2. **抓取** — 下载法规原文(网页 / PDF)
3. **分析** — 让 AI 判断每条法规的影响等级、适用产品、关键要求
4. **报告** — 生成可读的 Excel 周报

最终产出一份 Excel 表,告诉你"过去一段时间,全球出了哪些跟我们产品有关的法规、什么影响"。

---

## 当前版本:v1.0(性能与稳定性优化)

相比早期版本的关键改进:

- 💰 **跑一次成本降低约一半** — AI 调用关闭了不必要的"思考"开销,单次完整扫描从 ~$17 降到 ~$8-10(取决于扫描范围)
- ⚡ **API Key 配置错误立即提醒** — 没配 Gemini Key 时,程序启动那一刻就报清晰错误,不会跑半小时后才发现
- 📊 **更详细的成本统计** — 跑完末尾的 token 汇总新增缓存命中率、各阶段成本拆分,方便排查"哪步最贵"
- 🛡️ **去重逻辑更稳健** — 修复了人工补录场景下的边界 bug + 关键工具函数的跨模块依赖问题

更详细的版本说明见 git log。

---

## 两种使用方式,选一个

|  | 方案 A:GitHub 网页运行(推荐新手) | 方案 B:本地电脑运行 |
|---|---|---|
| 是否需要装 Python | ❌ 不需要 | ✅ 需要 |
| 是否需要懂命令行 | ❌ 完全不用 | ✅ 需要复制粘贴几条命令 |
| 跑在哪 | GitHub 服务器(免费) | 你的电脑 |
| 数据是否累积保留 | ⚠️ 每次跑完会清零 | ✅ 数据库累积保留 |
| 跑完报告怎么看 | 网页下载 zip | 直接打开 `reports/` 文件夹 |
| 适合谁 | 偶尔跑、不想装东西的人 | 想长期用、积累数据的人 |

---

# 方案 A:GitHub 网页运行(零基础推荐)

全程在浏览器里点点点,**不用装 Python、不用打开终端**。需要做的只有 4 件事:

1. Fork 这个项目到你自己的 GitHub
2. 申请一个免费的 Gemini API key
3. 把 key 填进你 fork 出的仓库的"保险箱"
4. 点一下"运行"按钮,等 10-30 分钟,下载报告

---

## A 第 1 步:注册 GitHub 账号(已有可跳过)

打开 https://github.com → 右上角 **Sign up** → 填邮箱、密码、用户名,一路下一步。
完全免费。

## A 第 2 步:Fork 本项目

1. 浏览器打开 👉
   https://github.com/michael909624/Global-Regulatory-Intelligence-System
2. 页面**右上角**找到一个 **Fork** 按钮(旁边带个分叉的小图标),点它
3. 弹出页面默认就行,点最下方绿色 **Create fork**
4. 几秒后页面跳转,网址会变成
   `https://github.com/你的用户名/Global-Regulatory-Intelligence-System` —
   这就是你自己的副本了

> 这一步把项目复制了一份到你账号下,跟原仓库相互独立,你的运行配额、数据都跟原作者无关。

## A 第 3 步:申请 Gemini API Key(免费)

1. 浏览器打开 https://aistudio.google.com/apikey
2. 用 Google 账号登录
3. 点 **Create API key** 按钮 → 选项目(没有就 "Create API key in new project")
4. 屏幕出现一串以 `AIza` 开头的字符串 — **完整复制**到记事本暂存

> Google 给的免费配额跑这个工具个人用一般够用,放心。

## A 第 4 步:把 Key 存进你 fork 出的仓库

⚠️ **重点**:一定要在**你自己 fork 的那个仓库**里操作,不是原作者的仓库。

1. 在你 fork 的仓库页面,顶部菜单点 **Settings**(设置,要往右拉一下才看得到)
2. 左侧菜单滚到 **Secrets and variables** → 点开 → 选 **Actions**
3. 页面右上角点绿色按钮 **New repository secret**
4. 填:
   - **Name**:`GEMINI_API_KEY`(必须**完全一致**,大小写、下划线都不能错)
   - **Secret**:粘贴你刚才复制的 key
5. 点 **Add secret**

完成后页面会显示 `GEMINI_API_KEY` 这一项,**已经加密**,任何人(包括你自己)都看不到具体内容,只能修改或删除。

## A 第 5 步:启用 Actions

Fork 出来的仓库默认 Actions 是**关闭**的,需要手动开一下:

1. 在你 fork 的仓库,顶部菜单点 **Actions**
2. 看到一个绿色提示框 "Workflows aren't being run on this forked repository" → 点
   **I understand my workflows, go ahead and enable them**(我理解,启用)

## A 第 6 步:运行!

1. 仍然在 **Actions** 页面,左侧菜单点 **运行 GRIS**
2. 右上角(列表上方)出现 **Run workflow** 按钮(灰色,带下拉箭头),点它
3. 弹出小框:
   - **Branch**:`main`(默认)
   - **运行命令**:第一次建议选 `status`(只查状态,几秒就完,验证配置正确)
4. 点绿色 **Run workflow**

页面 5 秒后刷新,看到一条**黄色转圈**的运行记录 — 它正在跑。

### 第一次先跑 `status` 确认环境 OK

进度变成 ✅ 绿勾 = 全部通过,环境配置成功。
变成 ❌ 红叉 = 哪一步出错了,点进去看报错。最常见原因:第 4 步的 Secret 名字写错。

### 然后跑真正的任务

再次回到 Actions → 运行 GRIS → Run workflow:

| 选项 | 用途 | 大约耗时 |
|------|------|---------|
| `run --quick` | 快速扫描(近 90 天新法规) | 5-15 分钟 |
| `run` | 完整扫描(全部法规) | 20-40 分钟 |
| `report` | 只重新生成 Excel(不重新抓数据) | 30 秒 |

## A 第 7 步:下载报告

跑完(✅ 绿勾)之后:

1. 点击那条运行记录进入详情页
2. 滚到页面**最底部**,有一个 **Artifacts** 区
3. 看到一个名字像 `gris-output-1` 的链接 → 点击下载(浏览器会下载一个 zip 文件)
4. 解压 zip,里面 `reports/` 文件夹下有 `.xlsx` Excel 文件 — 双击打开就是报告

⚠️ Artifact **7 天后自动删除**,要保留就及时下载到本地。

---

## 方案 A 注意事项

- ⚠️ **数据每次清零**:GitHub 服务器是临时的,每次运行从空数据库开始。
  这意味着你这次跑的结果跟上次完全独立,不会"越跑越多"。
- ⏱️ **配额**:Public 仓库的 Actions 完全免费、无限。Gemini API 看你 Google 账号的免费额度。
- 🔒 **隐私**:你的 API key 在 Secrets 里加密存储,即使有人看到你 fork 的代码,
  也读不到你的 key。

---

# 方案 B:本地电脑运行

适合**想长期用、累积数据**的人。比方案 A 多一些一次性配置。

## B 第 1 步:安装 Python

打开 https://www.python.org/downloads/ → 点黄色 **Download Python 3.x.x**:

- **Mac 用户**:双击 `.pkg` 文件 → 一路点"继续/同意/安装"
- **Windows 用户**:双击 `.exe` 文件 → 安装界面**最下方一定要勾选** `Add Python to PATH` → 点 Install Now

验证:

- **Mac**:`Command + 空格` → 输入 `terminal` → 回车 → 输入 `python3 --version` → 回车
- **Windows**:`Win 键` → 输入 `cmd` → 回车 → 输入 `python --version` → 回车

显示 `Python 3.x.x`(3.8 以上)就 OK。

## B 第 2 步:下载代码

打开 https://github.com/michael909624/Global-Regulatory-Intelligence-System
→ 点绿色 **Code** → **Download ZIP** → 解压到桌面或任意位置 → 文件夹名建议改成 `gris`(短一点好操作)。

## B 第 3 步:申请 Gemini API Key

同方案 A 的第 3 步:打开 https://aistudio.google.com/apikey 申请,复制 key。

## B 第 4 步:创建 `config_local.py` 配置文件

用记事本(或任何文本编辑器)在项目文件夹里**新建**一个文件,命名为 `config_local.py`。
内容只有一行:

```python
GEMINI_API_KEY = "粘贴你的key,保留两边的引号"
```

保存。例:

```python
GEMINI_API_KEY = "AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
```

> ⚠️ 这个文件不要发给别人、不要传到 GitHub — 里面是你的私人 key。

## B 第 5 步:安装依赖

在终端 / cmd 里**进入项目文件夹**(最简单办法:先输入 `cd ` 注意有空格,
然后把项目文件夹**直接拖**到终端窗口里,自动会粘贴路径,回车),然后运行:

- **Mac**: `pip3 install -r requirements.txt`
- **Windows**: `pip install -r requirements.txt`

约 1-2 分钟,看到 `Successfully installed ...` 就成功了。

## B 第 6 步:初始化数据库

- **Mac**: `python3 gris.py init`
- **Windows**: `python gris.py init`

## B 第 7 步:开始用

> Mac 用户把下面所有 `python` 替换成 `python3`。

```bash
python gris.py run --quick    # 快速扫描(近 90 天新法规,5-15 分钟)
python gris.py run            # 完整扫描(全部法规,20-40 分钟)
python gris.py status         # 看数据库统计
python gris.py view 高        # 终端查看 🔴 重要法规(支持 高/中)
python gris.py view_dropped   # 看 triage 预筛 drop 清单防误杀(可 --pursue <id> 恢复某条)
python gris.py consolidate    # 法规编号聚类:同 reg_id 软合并到主条目
python gris.py backfill       # 补全缺失的来源 URL(Gemini 逐条查找)
python gris.py reanalyze      # 重置「不相关」条目并重新分析
python gris.py report         # 重新生成 Excel(不重抓数据)
python gris.py                # 显示完整命令帮助
```

跑完后,Excel 报告自动生成在 `reports/` 文件夹,直接双击打开。

---

# 通用参考

## 输出在哪

| 内容 | 方案 A(网页) | 方案 B(本地) |
|------|---------------|---------------|
| Excel 周报 | Artifact zip → 解压后 `reports/` | 项目文件夹 `reports/` |
| 数据库 | 每次清零 | `data/gris.db`(累积) |
| 运行日志 | Artifact zip → 解压后 `logs/` | 项目文件夹 `logs/` |

## 影响等级标记

报告里你会看到:

- 🔴 **重要(L1)** — 影响产品研发设计/生产/市场准入(产品本身能否上市)
- 🟡 **次要(L2)** — 影响用户使用/销量/间接盈利(已上市产品能否卖好)

(原 🟢 低影响档已废弃 — 业务模型本质二元,中间档徒增噪音)

另外可能在"业务影响"列开头看到:

- ⚠️ **AI 合成分析** — 表示原文抓取失败(网站拦截 / JS 渲染壳 / 占位页等),
  分析内容是 AI 基于法规标题 + 公开背景知识合成的兜底版本,**不等同于原文**,
  关键决策请回到来源链接核对原文。

## 常见问题

### 方案 A 相关

**Q. 跑 Action 报红叉,提示 GEMINI_API_KEY 错误**
→ 第 4 步 Secret 名字必须是 `GEMINI_API_KEY`,大小写完全一致,前后不能有空格。

**Q. Action 页面没有 "运行 GRIS" 这一项**
→ 第 5 步还没启用 Actions,回去打开开关。

**Q. Run workflow 按钮点不到 / 灰色**
→ 你可能不是在自己 fork 的仓库里。确认网址是 `github.com/你的用户名/...`,
不是 `github.com/michael909624/...`。

### 方案 B 相关

**Q. 终端显示 `python: command not found` 或 `'python' 不是内部命令`**
→ Mac 用 `python3` 不要用 `python`;Windows 重装 Python 时勾选 `Add Python to PATH`。

**Q. `pip install` 装不上 / 报网络错误**
→ 用国内镜像:
```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**Q. 报错 `ModuleNotFoundError: config_local`**
→ `config_local.py` 没建好。检查:
- 文件名拼写对不对
- 不是 `config_local.py.txt`(Windows 易错,在文件夹"查看"里勾选"文件扩展名"再确认)
- 文件放在了项目根目录,不是子文件夹

**Q. 跑到一半网络断了 / 程序中断**
→ 直接重新运行 `python gris.py run`,程序会从中断处继续,已入库数据不丢。

**Q. 想清空所有数据从头来**
→ `python gris.py reset`,有确认提示,输入 `yes` 才会执行。

### 通用

**Q. Gemini 免费配额用完了**
→ 等 24 小时刷新,或在 Google AI Studio 升级到付费(个人用一般每月几块钱)。

---

## 文件结构(技术好奇者可看)

系统的核心思路是 **"4 步流水线 + 多个 AI 决策点"**:每一步都有规则 + AI 的双层防御,
信息从粗到细逐层过滤,既控成本又防漏球。

```
gris/
├── gris.py              # 主入口(命令分发)
├── researcher.py        # 第 1 步:Gemini 搜索发现 + URL 质量门
├── consolidator.py      #   Stage 0:法规编号软聚类(reg_id 规则 + LLM 语义残余兜底)
├── scraper.py           # 第 2 步:抓网页/PDF + 占位页/空壳页拒收
├── analyzer/            # 第 3 步:AI 合规分析(已拆包,多个 AI 决策点)
│   ├── llm_triage.py    #   scrape 前预筛:批量 drop 明显无关条目,省下游成本
│   ├── main.py          #   主分析:原文 → compliance_analysis(原文优先 + 30 天去重)
│   ├── fallback.py      #   抓取失败时的 Gemini grounded 合成兜底
│   ├── consolidation.py #   Stage 3 二次合并(reg_id 规则 + 维度软聚类 + 矛盾拒收)
│   ├── llm_dedup.py     #   终末轻量 LLM 语义去重(规则去不掉的残余)
│   ├── llm_priority.py  #   末端 AI 终审:批量判 L1/L2 重要度 + P0/P1 优先级
│   ├── priority.py      #   时间窗口判定 + LLM 失败的启发式兜底
│   ├── values.py        #   LLM 输出字段标准化 + 入库
│   ├── backfill.py      #   历史数据回填
│   └── _shared.py       #   公共常量、prompts、文本截断、并发锁
├── reporter.py          # 第 4 步:生成 Excel + ⚠️ 合成警告兜底显示
├── classify.py          # 影响等级分类(产品/市场标准化)
├── authority.py         # 监管机构权威度评分
├── ai_client.py         # Gemini 客户端封装(重试/退避/token 统计/成本估算)
├── seeds.py             # 启动时的种子法规库
├── database.py          # SQLite 数据库 schema + 查询 + 迁移
├── manual_input.py      # 抓取失败时人工补录
├── utils.py             # 通用工具(reg_id 归一化、JSON 容错解析等)
├── config.py            # 公共配置
├── config_local.py      # 你的 API key(自建,不要分享)
├── requirements.txt     # Python 依赖
├── prompts/             # 所有 LLM prompts(21 个独立文件,便于改写)
├── rules/               # 业务规则数据(法规别名、市场分级、导航词表等)
├── .github/workflows/   # GitHub Actions 配置(run.yml)
├── data/                # 数据库
├── logs/                # 日志
└── reports/             # Excel 输出
```

**Pipeline 全貌(10 个阶段,主要决策点都有 AI 参与)**:

| 阶段 | 模块 | 输入 | AI 决策 |
|------|------|------|---------|
| 1. 发现 | `researcher.py` | 议题列表 | Plan(发散议题)+ Fetch(grounded 搜索) |
| 1.5. 编号聚类 | `consolidator.py` | raw_search_results | reg_id 规则 + LLM 残余语义聚类 |
| 2. 抓取 | `scraper.py` | source_url | 无 AI(纯 HTTP/PDF) |
| 2.5. 预筛 | `analyzer/llm_triage.py` | raw 候选 | LLM 批判:pursue/drop |
| 3a. 主分析 | `analyzer/main.py` | scraped 原文 | LLM 抽客观字段(JSON) |
| 3b. 合成兜底 | `analyzer/fallback.py` | 抓取失败条目 | LLM grounded 合成 + 抽字段 |
| 3c. 整合 | `analyzer/consolidation.py` | compliance_analysis | reg_id 合并 + LLM 语义合并 |
| 3d. 终末去重 | `analyzer/llm_dedup.py` | 残余条目 | 轻量 LLM 语义近似 |
| 3e. 终审 | `analyzer/llm_priority.py` | 全部条目 | 批量判 L1/L2 + P0/P1 |
| 4. 报告 | `reporter.py` | 排序后条目 | 无 AI(纯 Excel 渲染) |

---

## 需要帮助?

跑不通?把报错截图整段发给项目作者,通常很快能解决。
