# rules/ — 业务规则配置文件

这个目录里的 `.txt` 文件让你不用懂代码就能直接修改 GRIS 系统的"业务规则"。
任何用记事本都能打开和编辑。改完保存,**重启 GRIS** 即生效。

## 文件清单

| 文件 | 作用 | 改动后影响 |
|---|---|---|
| `reg_aliases.txt` | 法规别名 → 内部归一名(如 PSTI / RoHS / REACH) | 同别名的不同写法会被聚到一起去重 |
| `reg_alias_to_celex.txt` | 法规别名 → CELEX 编号(如 CRA → EU/2024/2847) | 把"CRA"和"Reg 2024/2847"识别为同一份法规 |
| `navigation_titles.txt` | 导航/无义务页面关键词(如 press release / site map) | Stage 0 聚类时这类标题不会抢占 keeper 位 |
| `product_sort_order.txt` | 周报里产品列的排序(如 短交通 → ebike → 电摩 → 割草机) | 周报里同重要度同市场的法规按这个顺序排列 |
| `market_tiers.txt` | 市场分级占位(暂未接入代码,后续可能用) | 当前不影响任何行为 |

## 通用编辑规则

- **每行一条规则**
- **空行忽略**
- **`#` 开头整行 = 注释**(写给自己/同事看,系统不读)
- **UTF-8 编码**(中文法规名直接写,无需转义)
- 改完保存,重启系统(`python gris.py status` 跑通即说明加载成功)

## 单列文件格式(navigation_titles / product_sort_order)

每行一个关键词:

```
# 这是注释
press release
site map
glossary
```

## 映射文件格式(reg_aliases / reg_alias_to_celex)

每行 `<左侧> => <右侧>`,左右分别是"模式"和"值":

```
# 欧盟网络韧性法
CRA|CYBER\s+RESILIENCE\s+ACT => EU/2024/2847
```

**左侧支持正则**:
- `|` 表示"或"(`A|B` 命中 A 或 B)
- `\s+` 表示"一个或多个空格"
- `\b` 表示"单词边界"
- 大小写不敏感由系统调用层决定(consolidator 会先把标题转成大写再匹配)

**写错正则不会让系统崩**:启动时如果某一行正则编译失败,系统会在日志里
提醒(`rules/<name>.txt 正则编译失败 ...`),自动跳过该行,其他合法行
照常加载。所以放心改,改坏了顶多是那一行不生效,不会影响周报生成。

## 加新条目的安全做法

1. 找到对应文件(如要加新法规别名 → `reg_alias_to_celex.txt`)
2. 在文件末尾加一行,**先写注释说明这是什么**:
   ```
   # 数字服务法(2024 起欧盟通过)
   DSA|DIGITAL\s+SERVICES\s+ACT => EU/2022/2065
   ```
3. 保存,运行 `python gris.py status` 看不报错
4. 想看效果,跑 `python gris.py consolidate` 或 `python gris.py report`

## 工程常量(不要改的部分)

`.py` 代码里还有一些"工程常量"没有外移——比如分词停用词、`reg_id`
正则抽取兜底、矛盾措辞检测等。这些是"语言处理基础设施",不是业务规则,
改动需要评估对系统的影响。代码里这些位置都加了显式注释,看到
**`# 工程常量(非业务规则)`** 标记的就别动。
