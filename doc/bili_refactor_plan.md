# B站爬虫架构重构方案

## 目标

将 B站爬虫从多脚本架构（`momentum_spider.py` + `value_spider.py` + `util.py`），
改造为模仿小红书（`xhs_main.py` + `xhs_spider.py` + `xhs_util.py`）的三层统一架构。

**原则：只改结构，不改核心逻辑。**

## 重构后架构

### 文件结构
```
bili_main.py     ← 统一 CLI 入口（替代 momentum_spider + value_spider 的 main）
bili_spider.py   ← BiliSpider 类（合并原 BiliSpider + ValueSpider 的分析逻辑）
bili_util.py     ← 公共工具（原 util.py 重命名 + 新增价值分析DB方法）
```

旧文件直接删除：`momentum_spider.py`、`value_spider.py`、`util.py`

### 新命令风格
```bash
# 关键词搜索 + 动量分析
python bili_main.py -m momentum search -k 跨越阶级 -p 10 -t 5

# 关键词搜索 + 价值分析
python bili_main.py -m value search -k 穷人 -p 10 -t 5

# 全站热门 + 动量分析
python bili_main.py -m momentum search --popular -p 10 -t 5

# 全站热门 + 价值分析
python bili_main.py -m value search --popular -p 10 -t 5
```

### CLI 参数（完全复用原 momentum_spider / value_spider 的参数）
```
-m / --mode     分析模式: momentum | value（无默认，必选）
子命令:
  search
    -k keyword    关键词（--popular 时可选）
    -p pages      爬取页数 (默认5)
    -t threads    线程数 (默认3)
    -d delay      请求延迟 (默认1.0)
    -o order      排序方式 (默认click)
    --popular     全站热门模式
    --limit       显示数量 (默认全部，仅 value 模式)
```

## 模块职责

### bili_main.py
- 解析参数
- `from bili_spider import BiliSpider`
- 创建 BiliSpider → 根据 mode 执行对应流程：
  - momentum: crawl → enrich → analyze_momentum
  - value: crawl → enrich → analyze_value

### bili_spider.py
- BiliSpider 类，包含全部爬取/补全/分析逻辑
- 原 BiliSpider 的爬取、补全、动量分析逻辑不变
- 原 ValueSpider 的价值分析逻辑合并进来：analyze_value()、_print_tag_ranking()、_export_csv()

### bili_util.py
- CookieManager + WbiSigner + Database（与原 util.py 相同）
- 新增 get_keyword_videos_for_value()、export_value_csv() 方法

## 输出目录（与原来完全一致）
```
output/{关键词}/
  ├── bili_spider_{timestamp}.log          # 日志
  ├── momentum_{timestamp}.csv             # 动量分析CSV
  ├── value_{timestamp}.csv                # 价值分析CSV
  └── tags_{timestamp}.txt                 # 标签权重分析
```

## 实施步骤

### Step 1: 创建 bili_util.py ✅ 已完成
### Step 2: 创建 bili_spider.py
### Step 3: 创建 bili_main.py
### Step 4: 删除旧文件（momentum_spider.py、value_spider.py、util.py）
### Step 5: 更新 command 文件
### Step 6: 验证
