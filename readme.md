# 1. 爬取服饰关键词，5页，导出CSV
python bili_spider.py -k 服饰 -p 5 -e

# 2. 再次运行，会自动跳过今天已爬取的视频
python bili_spider.py -k 服饰 -p 5

# 3. 仅导出CSV（不爬取）
python bili_spider.py -k 服饰 --export-only

# 4. 爬取其他关键词
python bili_spider.py -k 穿搭 -p 3 -e