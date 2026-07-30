新算法 5 个评分维度：
播放速率 (30%)：play_nums / video_age_hours，用发布时间计算
粉丝转化率 (25%)：play_nums / uploader_fans，衡量出圈程度
互动密度 (20%)：(点赞+投币+收藏+评论+弹幕) / play_nums，衡量内容质量
新鲜度 (15%)：基于视频年龄的分段函数
播放量归一化 (10%)：同关键词下的相对位置

新增功能：
--momentum-only 参数：仅分析已有数据，不执行爬取
自动回填旧数据的 video_age_hours、play_velocity、engagement_score
多次爬取后自动显示真实历史增长率作为参考

新用法：
python bili_spider.py -k 服饰 -p 10 -m              # 爬取+立即分析
python bili_spider.py -k 服饰 --momentum-only  

python bili_spider.py -k 富人 -p 6

## 快手小样本搜索试验

快手模块借鉴 MediaCrawler 对 `visionSearchPhoto` 返回结构的理解，但由真实浏览器
发起请求并监听响应，不复制其 GraphQL 查询实现。当前只验证关键词搜索、作品基础
字段、关键词多对多关系、历史快照和简单动量排序，暂不采集评论。

```powershell
# 首次运行建议使用有头模式，最多采集 1 个批次
python -m src.kuaishou.kuaishou_main search -k "深圳便宜美食" -p 1 --headed

# 仅测试无需登录时能否得到搜索响应
python -m src.kuaishou.kuaishou_main search -k "深圳便宜美食" -p 1 --headed --no-login-gate

# 检查持久化登录状态
python -m src.kuaishou.kuaishou_main check-login
```

结果写入 `data/kuaishou_spider.db`，CSV 和日志写入对应关键词的 `output/` 目录。
