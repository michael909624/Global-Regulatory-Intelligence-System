# GRIS — 全球法规情报系统

一个自动追踪全球电动出行产品(电动滑板车、电动自行车、电动摩托车、智能割草机等)
最新法规动态的 Python 工具。

它会自动:

1. **发现** — 用 Gemini AI 搜索全球各国监管机构的最新法规
2. **抓取** — 下载法规原文(网页 / PDF)
3. **分析** — 让 AI 判断每条法规的影响等级、适用产品、关键要求
4. **报告** — 生成可读的 Excel 周报

最终产出一份 Excel 表,告诉你"过去一段时间,全球出了哪些跟我们产品有关的法规、什么影响"。

---

## 跑这个程序需要什么

- 一台电脑(Mac、Windows 都行)
- 大约 **15 分钟**做一次性配置
- 一个免费的 Google 账号(用来申请 Gemini API key,**完全免费**)

不用懂编程,跟着下面步骤一步一步来就行。

---

## 第一次配置(只需做一次)

### 第 1 步:安装 Python

打开浏览器,访问 https://www.python.org/downloads/

点页面上方大大的黄色按钮 **Download Python 3.x.x** 下载,然后:

- **Mac 用户**:双击下载的 `.pkg` 文件,一路点"继续/同意/安装"
- **Windows 用户**:双击下载的 `.exe` 文件,**安装界面最下方一定要勾选 `Add Python to PATH`**,然后点 Install Now

安装完成后,验证一下:

- **Mac**:按 `Command + 空格` → 输入 `terminal` → 回车 → 在终端里输入 `python3 --version` → 回车
- **Windows**:按 `Win 键` → 输入 `cmd` → 回车 → 在窗口里输入 `python --version` → 回车

如果显示 `Python 3.x.x`(任何 3.8 以上的版本都行)就说明装好了。

### 第 2 步:下载本项目代码

在浏览器打开:
https://github.com/michael909624/Global-Regulatory-Intelligence-System

点页面右上方绿色 **Code** 按钮 → **Download ZIP** → 把 zip 解压到一个你喜欢的位置
(例如桌面)。文件夹名建议改简单点,比如 `gris`。

### 第 3 步:申请 Gemini API Key(免费)

1. 浏览器打开 https://aistudio.google.com/apikey
2. 用 Google 账号登录
3. 点 **Create API key** 按钮
4. 选一个项目(没有的话点 "Create API key in new project")
5. 屏幕上会显示一串以 `AIza` 开头的字符串,**完整复制**到记事本保存好

> Google 给的免费配额一般个人用户跑这个工具完全够,放心用。

### 第 4 步:把 API Key 填进配置文件

用记事本(或任何文本编辑器)在项目文件夹里**新建**一个文件,命名为:

```
config_local.py
```

文件内容写入下面这两行(把引号里那一串替换成你刚才复制的 key):

```python
GEMINI_API_KEY = "粘贴你的key到这里,保留两边的引号"
```

完整示例:

```python
GEMINI_API_KEY = "AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
```

保存。

> ⚠️ 这个文件不要发给别人,也不要传到网上 — 里面是你的私人 key。

### 第 5 步:安装程序需要的工具包

在终端 / cmd 里,先**进入项目文件夹**。怎么进?最简单:

- 在终端 / cmd 里输入 `cd ` (注意 cd 后面有个空格,不要回车)
- 然后把项目文件夹**直接拖**到终端窗口里(自动会粘贴路径)
- 回车

进入文件夹后,运行:

- **Mac**: `pip3 install -r requirements.txt`
- **Windows**: `pip install -r requirements.txt`

会下载安装 5 个依赖包,大约 1-2 分钟。看到 `Successfully installed ...` 就成功了。

### 第 6 步:初始化数据库

继续在同一个终端窗口里运行:

- **Mac**: `python3 gris.py init`
- **Windows**: `python gris.py init`

看到一行成功提示就 OK。

---

## 开始使用

> 下面所有命令都是在**终端 / cmd 里、项目文件夹路径下**运行。
> Mac 用户把命令里的 `python` 全部替换为 `python3`。

### 最常用的两条命令

**完整扫描(推荐每周跑一次,大约 20-30 分钟):**

```bash
python gris.py run
```

**快速扫描(仅近 90 天新发布,大约 5-10 分钟):**

```bash
python gris.py run --quick
```

跑完后,Excel 报告会自动生成在 `reports/` 文件夹里,
直接双击打开就能看。

### 其他常用命令

```bash
python gris.py status        # 看数据库现在有多少条数据
python gris.py view          # 在终端里查看最近 20 条结果
python gris.py view 高       # 只看高影响法规
python gris.py view 高 50    # 高影响,最多 50 条
python gris.py report        # 重新生成 Excel 周报(不重新抓数据)
python gris.py                # 显示所有命令的帮助
```

### 程序流程拆解(可选了解)

`run` 命令其实是把 4 步连起来跑,你也可以分开跑:

| 步骤 | 命令 | 做什么 |
|------|------|--------|
| 1 | `python gris.py research` | 调用 Gemini 搜索全球新法规链接 |
| 2 | `python gris.py scrape` | 下载这些链接的原文(HTML / PDF) |
| 3 | `python gris.py analyze` | 让 AI 分析原文,判断影响 |
| 4 | `python gris.py report` | 生成 Excel 周报 |

---

## 输出在哪

| 内容 | 位置 |
|------|------|
| Excel 周报 | `reports/` 文件夹 |
| 数据库(累积所有历史数据) | `data/gris.db` |
| 运行日志(出问题时排查用) | `logs/` 文件夹 |

---

## 常见问题

### Q1. 终端里显示 `python: command not found` 或 `'python' 不是内部命令`

- **Mac**:用 `python3` 不要用 `python`
- **Windows**:Python 安装时没勾 `Add Python to PATH`,重装 Python,这次记得勾上

### Q2. `pip install` 报错 / 装不上

网络问题居多。换个网络试试,或者使用国内镜像:

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q3. 跑 `run` 报错 `GEMINI_API_KEY` 相关错误

检查 `config_local.py` 里:
- key 两边的双引号没丢
- key 完整,没多空格、没多换行
- 文件名是 `config_local.py`,不是 `config_local.py.txt`(Windows 容易出这问题,
  在文件夹的"查看"里勾上"文件扩展名"再检查一下)

### Q4. 跑到一半网络断了 / 程序中断

直接重新运行 `python gris.py run` 就行,程序会从中断的地方继续,
已经入库的数据不会丢。

### Q5. 想重新开始,清空所有数据

```bash
python gris.py reset
```

会有确认提示,输入 `yes` 才会真正清空。

### Q6. 我的 Gemini 免费配额用完了怎么办

等 24 小时配额刷新,或者去 Google AI Studio 升级到付费(便宜,
个人用一般每月几块钱)。

---

## 文件结构说明(技术好奇者可看)

```
gris/
├── gris.py              # 主入口,所有命令都从这里调度
├── researcher.py        # 第 1 步:Gemini 搜索发现
├── scraper.py           # 第 2 步:抓取网页/PDF 原文
├── analyzer.py          # 第 3 步:AI 分析合规影响
├── reporter.py          # 第 4 步:生成 Excel 报告
├── classify.py          # 影响等级分类规则
├── database.py          # SQLite 数据库操作
├── manual_input.py      # 抓取失败时人工补录工具
├── utils.py             # 通用工具函数
├── config.py            # 公共配置(产品线、路径等)
├── config_local.py      # 你的 API key(自己创建,不要分享)
├── requirements.txt     # Python 依赖列表
├── data/                # 数据库存放处
├── logs/                # 运行日志
└── reports/             # Excel 周报输出
```

---

## 需要帮助?

跑不起来?直接把终端报错信息整段截图发给项目作者,通常 2 分钟能解决。
