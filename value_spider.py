"""
value_spider.py - B站视频价值评分爬虫

与 bili_spider.py（动量分析）并行，专注于识别"常青价值型"视频。
动量衡量的是"速度"（播放速率、新鲜度），价值衡量的是"质量"（深度互动、收藏价值）。

用法:
  python value_spider.py -k 穷人 -p 10 -t 5        # 爬取10页5线程+价值评分+导出
  python value_spider.py -k 服饰 -p 5              # 爬取5页3线程(默认)
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime

from util import DB_FILE, COOKIE_FILE, Database
from momentum_spider import BiliSpider


def _safe_print(text):
    """安全打印，处理终端编码不支持的字符"""
    try:
        print(text)
    except UnicodeEncodeError:
        safe = text.encode(sys.stdout.encoding or 'gbk', errors='replace').decode(sys.stdout.encoding or 'gbk', errors='replace')
        print(safe)


def _safe_str(s, max_len=26):
    """安全截断字符串，移除终端无法显示的字符"""
    if not s:
        return ""
    s = re.sub(r'[\U00010000-\U0010ffff]', '', s)
    return s[:max_len]


class ValueSpider:
    """价值评分爬虫，复用 BiliSpider 爬取，再做价值评分"""

    def __init__(self, args):
        self.args = args
        self.spider = BiliSpider(args)
        self.db = self.spider.db

    def run(self):
        """完整流程: 爬取 → 补粉丝 → 补标签 → 价值评分 → 展示 → 导出CSV"""
        # Step 1: 爬取数据
        self.spider.run()

        # Step 2: 补充粉丝数
        keyword = self.args.keyword
        self.spider.enrich_videos_with_fans(keyword)

        # Step 3: 补充标签
        self.spider.enrich_videos_with_tags(keyword)

        # Step 4: 价值评分
        self.analyze_value(keyword)

    def analyze_value(self, keyword):
        """对该关键词下的所有视频做价值评分"""
        # 查询该关键词下的所有视频（不限 task_id，爬多少算多少）
        cursor = self.db.conn.cursor()
        cursor.execute("""
            SELECT v.av_id, v.title, v.play_nums, v.uploader, v.uploader_uid, v.uploader_fans,
                   v.like_count, v.coin, v.favorites, v.share, v.danmakus, v.review,
                   v.video_age_hours, v.pubdate, v.tags
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ?
        """, (keyword,))
        rows = cursor.fetchall()

        if not rows:
            self.spider.log("[价值分析] 无数据可分析")
            return

        # 计算每个视频的价值维度
        videos = []
        for row in rows:
            av_id, title, play_nums, uploader, uploader_uid, uploader_fans, \
                like_count, coin, favorites, share_count, danmakus, review, \
                video_age_hours, pubdate, tags_str = row

            # 确保数值类型
            play_nums = float(play_nums or 0)
            uploader_fans = float(uploader_fans or 0)
            like_count = float(like_count or 0)
            coin = float(coin or 0)
            favorites = float(favorites or 0)
            share_count = float(share_count or 0)
            danmakus = float(danmakus or 0)
            review = float(review or 0)
            video_age_hours = float(video_age_hours or 0)

            # 5个维度原始值
            deep_ratio = (coin + favorites) / max(like_count, 1)
            total_interaction = like_count + coin + favorites + review + danmakus
            engagement_density = total_interaction / max(play_nums, 1)
            fav_rate = favorites / max(play_nums, 1)
            conv_rate = play_nums / max(uploader_fans, 1) if uploader_fans > 0 else 0
            share_rate = share_count / max(play_nums, 1)

            videos.append({
                "av_id": av_id,
                "title": title,
                "play_nums": play_nums,
                "uploader": uploader,
                "uploader_uid": uploader_uid,
                "uploader_fans": uploader_fans,
                "like_count": like_count,
                "coin": coin,
                "favorites": favorites,
                "share": share_count,
                "danmakus": danmakus,
                "review": review,
                "video_age_hours": video_age_hours,
                "pubdate": pubdate,
                "tags": tags_str or "",
                "tag_list": [t.strip() for t in (tags_str or "").split(",") if t.strip()],
                "deep_ratio": deep_ratio,
                "engagement_density": engagement_density,
                "fav_rate": fav_rate,
                "conv_rate": conv_rate,
                "share_rate": share_rate,
            })

        # 归一化 (min-max 到 [0,1])
        def normalize(values):
            min_v = min(values)
            max_v = max(values)
            if max_v == min_v:
                return [0.5] * len(values)
            return [(v - min_v) / (max_v - min_v) for v in values]

        deep_norm = normalize([v["deep_ratio"] for v in videos])
        eng_norm = normalize([v["engagement_density"] for v in videos])
        fav_norm = normalize([v["fav_rate"] for v in videos])
        conv_norm = normalize([v["conv_rate"] for v in videos])
        share_norm = normalize([v["share_rate"] for v in videos])

        for i, v in enumerate(videos):
            v["deep_score"] = deep_norm[i]
            v["eng_score"] = eng_norm[i]
            v["fav_score"] = fav_norm[i]
            v["conv_score"] = conv_norm[i]
            v["share_score"] = share_norm[i]
            v["value_score"] = (
                deep_norm[i] * 0.35 +
                eng_norm[i] * 0.25 +
                fav_norm[i] * 0.20 +
                conv_norm[i] * 0.10 +
                share_norm[i] * 0.10
            )

        # 按价值分排序
        videos.sort(key=lambda x: x["value_score"], reverse=True)

        # 展示
        self._print_ranking(videos, keyword)

        # 标签权重分析
        self._print_tag_ranking(videos, keyword)

        # 导出CSV
        self._export_csv(videos, keyword)

    def _print_ranking(self, videos, keyword):
        """打印价值排名表"""
        self.spider.log(f"\n{'=' * 100}")
        self.spider.log(f"  价值分析 (独立评分)")
        self.spider.log(f"{'=' * 100}")
        self.spider.log(f"关键词: {keyword}")
        self.spider.log(f"评分维度: 深度互动比(35%) + 互动密度(25%) + 收藏率(20%) + 粉丝转化(10%) + 分享率(10%)")
        self.spider.log(f"说明: 价值评分衡量视频的长期内容质量，不受视频年龄/新鲜度影响")
        self.spider.log("")

        limit = min(self.args.limit if hasattr(self.args, 'limit') else 999999, len(videos))
        self.spider.log(f"\n价值排行 Top {limit}（按价值综合评分排序）")
        self.spider.log("-" * 190)
        header = (
            f"{'排名':<4} {'标题':<28} {'播放量':>10} {'UP主':<10} {'粉丝':>8} "
            f"{'深/浅比':>7} {'互动密度':>8} {'收藏率':>7} {'转化':>7} {'分享率':>7} "
            f"{'深互动分':>7} {'互动分':>6} {'收藏分':>6} {'转化分':>6} {'分享分':>6} {'价值分':>7}"
        )
        self.spider.log(header)
        self.spider.log("-" * 190)

        for i, item in enumerate(videos[:limit], 1):
            title = _safe_str(item["title"] or "未知", 26)
            current = f"{int(item['play_nums']):,}" if item['play_nums'] else "-"
            uploader = _safe_str(item.get('uploader') or item.get('uploader_uid') or '未知', 9)
            fans = f"{int(item['uploader_fans']):,}" if item.get('uploader_fans') else "-"

            like = item.get('like_count', 0)
            deep = item.get('coin', 0) + item.get('favorites', 0)
            deep_light = f"{deep / max(like, 1):.2f}" if like > 0 else "-"

            engagement = f"{item['engagement_density']:.3f}" if item.get('engagement_density', 0) > 0 else "-"
            fav_rate = f"{item['fav_rate']:.3f}" if item.get('fav_rate', 0) > 0 else "-"
            conv = f"{item['conv_rate']:.1f}x" if item.get('conv_rate', 0) > 0 else "-"
            share_rate = f"{item['share_rate']:.3f}" if item.get('share_rate', 0) > 0 else "-"

            deep_score = f"{item['deep_score']:.3f}"
            eng_score = f"{item['eng_score']:.3f}"
            fav_score = f"{item['fav_score']:.3f}"
            conv_score = f"{item['conv_score']:.3f}"
            share_score = f"{item['share_score']:.3f}"
            value_score = f"{item['value_score']:.3f}"

            self.spider.log(
                f"{i:<4} {title:<28} {current:>10} {uploader:<10} {fans:>8} "
                f"{deep_light:>7} {engagement:>8} {fav_rate:>7} {conv:>7} {share_rate:>7} "
                f"{deep_score:>7} {eng_score:>6} {fav_score:>6} {conv_score:>6} {share_score:>6} {value_score:>7}"
            )

        self.spider.log("-" * 190)
        self.spider.log("\n提示: 价值分 = 深度互动比(35%) + 互动密度(25%) + 收藏率(20%) + 粉丝转化(10%) + 分享率(10%)")
        self.spider.log("      深度互动比 = (投币+收藏)/点赞，>1.0 表示用户愿意付出成本的认可超过浅层点赞")
        self.spider.log("      与动量分析互补：动量看'速度'，价值看'质量'")

    def _print_tag_ranking(self, videos, keyword):
        """
        标签权重算法:
        对每个标签，统计含有该标签的视频数量（出现频次）和这些视频的平均价值分。
        标签权重 = 出现频次 × 平均价值分
        含义: 既考虑标签的普遍性（多少视频有这个标签），也考虑标签关联视频的质量。
        """
        from collections import defaultdict

        total_videos = len(videos)
        tag_stats = defaultdict(lambda: {"count": 0, "score_sum": 0.0})

        for v in videos:
            for tag in v["tag_list"]:
                tag_stats[tag]["count"] += 1
                tag_stats[tag]["score_sum"] += v["value_score"]

        if not tag_stats:
            self.spider.log("\n[标签分析] 无标签数据")
            return

        # 计算权重: 频次 × 平均价值分
        tag_weights = []
        for tag, stats in tag_stats.items():
            avg_score = stats["score_sum"] / stats["count"]
            weight = stats["count"] * avg_score
            tag_weights.append({
                "tag": tag,
                "count": stats["count"],
                "avg_score": avg_score,
                "weight": weight,
                "pct": stats["count"] / total_videos * 100,  # 覆盖率
            })

        tag_weights.sort(key=lambda x: x["weight"], reverse=True)
        top10 = tag_weights[:10]

        self.spider.log(f"\n{'=' * 80}")
        self.spider.log(f"  标签权重 Top 10 (关键词: {keyword})")
        self.spider.log(f"{'=' * 80}")
        self.spider.log(f"总视频数: {total_videos} | 不同标签数: {len(tag_stats)}")
        self.spider.log(f"算法: 标签权重 = 出现视频数 × 平均价值分")
        self.spider.log(f"{'-' * 80}")
        self.spider.log(f"{'排名':<4} {'标签':<20} {'出现次数':>8} {'覆盖率':>8} {'平均价值分':>10} {'标签权重':>10}")
        self.spider.log(f"{'-' * 80}")
        for i, tw in enumerate(top10, 1):
            tag_display = _safe_str(tw["tag"], 18)
            self.spider.log(
                f"{i:<4} {tag_display:<20} {tw['count']:>8} {tw['pct']:>7.1f}% {tw['avg_score']:>10.3f} {tw['weight']:>10.3f}"
            )
        self.spider.log(f"{'-' * 80}")
        self.spider.log("说明: 标签权重越高 = 该标签在高质量视频中出现越频繁，越值得重点关注")

    def _export_csv(self, videos, keyword):
        """导出价值分析结果到 CSV"""
        output_dir = os.path.join("output", keyword)
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"value_{timestamp}.csv")

        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "排名", "av_id", "标题", "播放量", "UP主", "UP主UID", "粉丝数",
                "点赞数", "投币数", "收藏数", "分享数", "弹幕数", "评论数",
                "标签", "深度互动比(币+藏/赞)", "互动密度", "收藏率", "粉丝转化率", "分享率",
                "深度互动得分", "互动密度得分", "收藏率得分", "转化率得分", "分享率得分",
                "视频年龄(小时)", "发布时间", "价值综合评分"
            ])
            for i, item in enumerate(videos, 1):
                writer.writerow([
                    i,
                    item["av_id"],
                    item["title"],
                    int(item.get("play_nums", 0)),
                    item.get("uploader", ""),
                    item.get("uploader_uid", ""),
                    int(item.get("uploader_fans", 0)),
                    int(item.get("like_count", 0)),
                    int(item.get("coin", 0)),
                    int(item.get("favorites", 0)),
                    int(item.get("share", 0)),
                    int(item.get("danmakus", 0)),
                    int(item.get("review", 0)),
                    item.get("tags", ""),
                    round(item.get("deep_ratio", 0), 4),
                    round(item.get("engagement_density", 0), 4),
                    round(item.get("fav_rate", 0), 4),
                    round(item.get("conv_rate", 0), 4),
                    round(item.get("share_rate", 0), 4),
                    round(item.get("deep_score", 0), 4),
                    round(item.get("eng_score", 0), 4),
                    round(item.get("fav_score", 0), 4),
                    round(item.get("conv_score", 0), 4),
                    round(item.get("share_score", 0), 4),
                    round(item.get("video_age_hours", 0), 2),
                    item.get("pubdate", ""),
                    round(item.get("value_score", 0), 4),
                ])

        self.spider.log(f"\n[导出] 价值分析结果已保存到 {output_file}")

    def close(self):
        self.spider._close_playwright()
        self.db.close()


def main():
    parser = argparse.ArgumentParser(
        description="B站视频价值评分爬虫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python value_spider.py -k 穷人 -p 10 -t 5        # 爬取10页5线程+价值评分+导出
  python value_spider.py -k 服饰 -p 5              # 爬取5页+价值评分

说明:
  与 bili_spider.py 相同的爬取流程，但评分维度不同:
  - bili_spider: 动量分析（播放速率、新鲜度）→ 找"正在爆"的视频
  - value_spider: 价值评分（深度互动、收藏率）→ 找"值得看"的视频
        """
    )

    parser.add_argument("--keyword", "-k", type=str, default="服饰",
                        help="搜索关键词 (默认: 服饰)")
    parser.add_argument("--pages", "-p", type=int, default=5,
                        help="爬取页数 (默认: 5)")
    parser.add_argument("--order", "-o", type=str, default="click",
                        choices=["click", "pubdate", "dm", "stow"],
                        help="排序方式 (默认: click)")
    parser.add_argument("--threads", "-t", type=int, default=3,
                        help="线程数 (默认: 3)")
    parser.add_argument("--delay", "-d", type=float, default=1.0,
                        help="请求间隔秒数 (默认: 1.0)")
    parser.add_argument("--limit", "-l", type=int, default=999999,
                        help="显示数量 (默认: 全部)")

    args = parser.parse_args()

    spider = ValueSpider(args)
    try:
        spider.run()
    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止")
        spider.db.update_task(spider.spider.task_id, status="failed", error_msg="用户中断")
    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()
        if spider.spider.task_id:
            spider.db.update_task(spider.spider.task_id, status="failed", error_msg=str(e))
    finally:
        spider.close()


if __name__ == "__main__":
    main()
