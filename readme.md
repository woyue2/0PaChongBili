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