"""
bili_main.py - B站视频爬虫统一命令行入口
用法:
  python bili_main.py -m search -k 跨越阶级 -p 10 -t 5        # 同时跑动量+价值分析
  python bili_main.py -m momentum search -k 跨越阶级 -p 10 -t 5  # 只跑动量分析
  python bili_main.py -m value search -k 穷人 -p 10 -t 5         # 只跑价值分析
  python bili_main.py -m search --popular -p 10 -t 5             # 全站热门+双分析
"""

import argparse
import sys
import os

# 确保能 import src/ 下的模块（支持从根目录或 src/ 下运行 main.py）
# __file__ = src/bili/bili_main.py，向上两级得到项目根目录
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def add_search_args(parser):
    """给 parser 添加搜索相关参数"""
    parser.add_argument("-k", "--keyword", type=str, default="服饰",
                        help="搜索关键词 (默认: 服饰)")
    parser.add_argument("-p", "--pages", type=int, default=5,
                        help="爬取页数 (默认: 5)")
    parser.add_argument("-t", "--threads", type=int, default=3,
                        help="线程数 (默认: 3)")
    parser.add_argument("-d", "--delay", type=float, default=1.0,
                        help="请求间隔秒数 (默认: 1.0)")
    parser.add_argument("-o", "--order", type=str, default="click",
                        choices=["click", "pubdate", "dm", "stow"],
                        help="排序方式 (默认: click)")
    parser.add_argument("--popular", action="store_true",
                        help="全站热门模式：通过热门API爬取全站热门视频（无需关键词）")
    parser.add_argument("--limit", type=int, default=999999,
                        help="显示数量 (默认: 全部，仅value模式)")


def main():
    parser = argparse.ArgumentParser(description="B站视频爬虫 - 动量/价值双评分体系")

    parser.add_argument("-m", "--mode", required=True,
                        choices=["search", "momentum", "value"],
                        help="分析模式: search双分析, momentum动量, value价值")

    # 搜索参数加到主 parser，-m search 时直接使用
    add_search_args(parser)

    # 子命令（momentum/value 模式使用）
    sub = parser.add_subparsers(dest="command", help="子命令")
    search_p = sub.add_parser("search", help="搜索爬取 + 自动分析")
    add_search_args(search_p)  # 子命令也添加相同参数

    args = parser.parse_args()

    from bili_spider import BiliSpider
    from src.common import paths

    keyword = "全站热门" if args.popular else args.keyword
    output_dir = paths.output(keyword)
    os.makedirs(output_dir, exist_ok=True)

    # -m search 时，mode 设为 "both" 表示双分析
    mode = "both" if args.mode == "search" else args.mode
    spider = BiliSpider(args, output_dir=output_dir, mode=mode)

    try:
        # Step 1: 爬取
        if args.popular:
            spider.run_popular()
        else:
            spider.run()

        # Step 2: 数据补全
        spider.fix_comment_count_from_review()
        kw = None if args.popular else args.keyword
        spider.enrich_videos_with_fans(kw)
        spider.enrich_videos_with_tags(kw if not args.popular else "全站热门")

        # Step 3: 分析
        if args.mode == "search":
            spider.analyze_momentum()
            spider.analyze_value()
        elif args.mode == "momentum":
            spider.analyze_momentum()
        elif args.mode == "value":
            spider.analyze_value()

    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止")
        if spider.task_id:
            spider.db.update_task(spider.task_id, status="failed", error_msg="用户中断")
    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()
        if spider.task_id:
            spider.db.update_task(spider.task_id, status="failed", error_msg=str(e))
    finally:
        spider.close()


if __name__ == "__main__":
    main()
