import sqlite3

conn = sqlite3.connect("bili_spider.db")
cursor = conn.cursor()

# 获取所有需要补充粉丝数的 UID
cursor.execute("""
    SELECT DISTINCT uploader_uid, uploader 
    FROM bili_videos 
    WHERE (uploader_fans IS NULL OR uploader_fans = 0)
      AND uploader_uid IS NOT NULL 
      AND uploader_uid != ''
    LIMIT 10
""")

rows = cursor.fetchall()
print("需要补充粉丝数的前10个UP主:")
for uid, name in rows:
    print(f"  UID: {uid}, 名称: {name}")

conn.close()