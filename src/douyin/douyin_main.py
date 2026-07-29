"""
douyin_main.py - 抖音爬虫命令行入口
用法:
  python douyin_main.py -m momentum search -k 美食 -p 2
  python douyin_main.py -m value search -k 科技 -p 3
  python douyin_main.py -m both search -k 旅行 -p 2

  单独分析（数据库已有数据）:
    python douyin_main.py -m momentum -k 美食 --csv output.csv
    python douyin_main.py -m value -k 美食 --csv output.csv

  登录态检测:
    python douyin_main.py check-login
    # search 每次启动都会自动验证；超过3天或会话失效时强制扫码刷新

输出目录:
  ./output/{关键词}/
    ├── douyin_spider_*.log
    ├── momentum_*.csv
    └── value_*.csv
"""

import argparse
import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description="抖音爬虫 - Playwright 监听 API 模式")
    parser.add_argument("-m", "--mode", default="both",
                        choices=["momentum", "value", "both"],
                        help="分析模式: momentum动量(默认), value价值, both全部")
    parser.add_argument("-k", "--keyword", help="关键词（仅单独分析时用）")
    parser.add_argument("--csv", help="单独分析时导出 CSV 路径")

    sub = parser.add_subparsers(dest="command", help="子命令")

    search_p = sub.add_parser("search", help="搜索爬取视频 + 自动分析")
    search_p.add_argument("-k", "--keyword", required=True, help="搜索关键词")
    search_p.add_argument("-p", "--pages", type=int, default=3, help="爬取页数 (默认3)")
    search_p.add_argument("--headed", action="store_true", help="有头模式（显示浏览器，默认无头）")

    sub.add_parser("check-login", help="检测登录态是否有效")

    args = parser.parse_args()

    if not args.command and not args.keyword:
        parser.print_help()
        return

    try:
        from .douyin_spider import DouyinSpider
    except ImportError:
        from douyin_spider import DouyinSpider

    if args.command == "check-login":
        spider = DouyinSpider(args)
        try:
            ok = spider.check_login()
            if ok:
                print("\n登录态有效 ✓")
            else:
                print("\n登录态失效 ✗；运行 search 时会自动打开浏览器要求扫码刷新")
                sys.exit(1)
        finally:
            spider.close()

    elif args.command == "search":
        args.headless = not args.headed
        from src.common import paths
        output_dir = paths.output(args.keyword)
        spider = DouyinSpider(args, output_dir=output_dir, mode=args.mode)
        try:
            if not spider.ensure_login_ready():
                print("[错误] 登录刷新失败，已停止本次抓取")
                sys.exit(1)
            spider.crawl_keyword(
                keyword=args.keyword,
                pages=args.pages,
            )
        except Exception as e:
            print(f"[错误] 爬取过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            spider.close()

    elif args.keyword:
        mode = args.mode
        spider = DouyinSpider(args)
        try:
            if mode in ("momentum", "both"):
                csv_path = args.csv if args.csv else None
                spider.momentum_analysis(args.keyword, csv_file=csv_path)
            if mode in ("value", "both"):
                csv_path = args.csv if args.csv else None
                if mode == "value" or (mode == "both" and args.csv):
                    spider.value_analysis(args.keyword, csv_file=csv_path)
        finally:
            spider.close()


if __name__ == "__main__":
    main()
