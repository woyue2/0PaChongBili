"""快手关键词搜索小样本试验命令行入口。"""

import argparse
import os
import sys


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)


def main():
    parser = argparse.ArgumentParser(
        description="快手小样本搜索试验（浏览器响应监听模式）"
    )
    subparsers = parser.add_subparsers(dest="command")

    search_parser = subparsers.add_parser("search", help="搜索并保存快手作品")
    search_parser.add_argument("-k", "--keyword", required=True)
    search_parser.add_argument(
        "-p", "--pages", type=int, default=1, help="采集批次数，默认1"
    )
    display = search_parser.add_mutually_exclusive_group()
    display.add_argument("--headed", dest="headless", action="store_false")
    display.add_argument("--headless", dest="headless", action="store_true")
    search_parser.set_defaults(headless=False)
    search_parser.add_argument(
        "--keep-open", action="store_true", help="完成后保留浏览器窗口"
    )
    search_parser.add_argument(
        "--no-login-gate",
        action="store_true",
        help="跳过登录门禁，仅验证匿名搜索是否可用",
    )

    login_parser = subparsers.add_parser("check-login", help="检查快手登录状态")
    login_parser.set_defaults(headless=False)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    from src.common import paths
    from src.kuaishou.kuaishou_spider import KuaishouSpider

    output_dir = (
        paths.output(args.keyword) if args.command == "search" else None
    )
    spider = KuaishouSpider(args, output_dir=output_dir)
    try:
        if args.command == "check-login":
            ok = spider.check_login()
            print("登录状态有效" if ok else "未检测到登录状态")
            raise SystemExit(0 if ok else 1)

        if not args.no_login_gate and not spider.ensure_login_ready():
            print("快手登录失败，停止本次小样本试验")
            raise SystemExit(1)
        videos = spider.crawl_keyword(args.keyword, pages=max(args.pages, 1))
        print(f"本次快手试验获得 {len(videos)} 条作品")
    finally:
        if getattr(args, "keep_open", False):
            spider.wait_for_manual_close()
        else:
            spider.close()


if __name__ == "__main__":
    main()
