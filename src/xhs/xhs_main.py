"""
xhs_main.py - 小红书爬虫命令行入口
用法:
  python xhs_main.py -m momentum search -k 跨越阶级 -p 2
  python xhs_main.py -m value search -k 美食 -p 3
  python xhs_main.py -m both search -k 穿搭 -p 2

  单独分析（数据库已有数据）:
    python xhs_main.py -m momentum -k 美食 --csv output.csv
    python xhs_main.py -m value -k 美食 --csv output.csv

  登录态检测:
    python xhs_main.py check-login
    # search 每次启动都会自动验证；超过3天或会话失效时强制扫码刷新

  补抓缺失分享链接:
    python xhs_main.py retry-links --task-id 123 --headed

输出目录:
  ./output/{关键词}/
    ├── xhs_spider_20260725_043745.log
    ├── momentum_20260725_043745.csv
    └── value_20260725_043745.csv
"""

import argparse
import sys
import os

# 确保能 import src/ 下的模块（支持从根目录或 src/ 下运行 main.py）
# __file__ = src/xhs/xhs_main.py，向上两级得到项目根目录
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description="小红书爬虫 - Playwright 监听 API 模式")
    parser.add_argument("-m", "--mode", default="both",
                        choices=["momentum", "value", "both"],
                        help="分析模式: momentum动量(默认), value价值, both全部")
    parser.add_argument("-k", "--keyword", help="关键词（仅单独分析时用）")
    parser.add_argument("--csv", help="单独分析时导出 CSV 路径")

    sub = parser.add_subparsers(dest="command", help="子命令")

    search_p = sub.add_parser("search", help="搜索爬取笔记 + 自动分析")
    search_p.add_argument("-k", "--keyword", required=True, help="搜索关键词")
    search_p.add_argument("-p", "--pages", type=int, default=3, help="爬取页数 (默认3)")
    search_p.add_argument("-s", "--sort", default="general",
                          choices=["general", "popular", "new"],
                          help="排序方式: general综合(默认), popular最热, new最新")
    search_p.add_argument("--headed", action="store_true", help="有头模式（显示浏览器，默认无头）")

    sub.add_parser("check-login", help="检测登录态是否有效")
    retry_p = sub.add_parser("retry-links", help="只补指定任务中缺失的分享链接")
    retry_p.add_argument("--task-id", type=int, required=True, help="原抓取任务 ID")
    retry_p.add_argument("--limit", type=int, help="本次最多补多少条")
    retry_p.add_argument("--headed", action="store_true", help="有头模式（显示浏览器）")

    args = parser.parse_args()

    if not args.command and not args.keyword:
        parser.print_help()
        return

    from xhs_spider import XhsSpider

    if args.command == "check-login":
        spider = XhsSpider(args)
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
        spider = XhsSpider(args, output_dir=output_dir, mode=args.mode)
        try:
            if not spider.ensure_login_ready():
                print("[错误] 登录刷新失败，已停止本次抓取")
                sys.exit(1)
            spider.crawl_keyword(
                keyword=args.keyword,
                pages=args.pages,
                sort=args.sort,
            )
        except Exception as e:
            print(f"[错误] 爬取过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            spider.close()

    elif args.command == "retry-links":
        args.headless = not args.headed
        spider = XhsSpider(args)
        try:
            if not spider.ensure_login_ready():
                print("[错误] 登录刷新失败，已停止补链接")
                sys.exit(1)
            spider.retry_missing_links(args.task_id, limit=args.limit)
        finally:
            spider.close()

    elif args.keyword:
        mode = args.mode
        spider = XhsSpider(args)
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
