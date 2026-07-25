"""
xhs_main.py - 小红书爬虫命令行入口
用法:
  python xhs_main.py -m momentum search -k 跨越阶级 -p 2
  python xhs_main.py -m value search -k 美食 -p 3
  python xhs_main.py -m both search -k 穿搭 -p 2

  单独分析（数据库已有数据）:
    python xhs_main.py -m momentum -k 美食 --csv output.csv
    python xhs_main.py -m value -k 美食 --csv output.csv

  登录:
    python xhs_main.py login
    python xhs_main.py check-login

输出目录:
  ./output/{关键词}/
    ├── xhs_spider_20260725_043745.log
    ├── momentum_20260725_043745.csv
    └── value_20260725_043745.csv
"""

import argparse
import sys
import os

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
    sub.add_parser("login", help="打开浏览器手动登录并保存 cookie")

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
                print("\n登录态失效 ✗，请运行: python xhs_main.py login")
                sys.exit(1)
        finally:
            spider.close()

    elif args.command == "login":
        import time
        from playwright.sync_api import sync_playwright

        print("=" * 60)
        print("  小红书扫码登录")
        print("=" * 60)
        print()
        print("  浏览器将打开，请手动扫码登录")
        print("  登录成功后，cookie 会自动保存到 xhs_cookie.txt")
        print("  登录完成后，按 Enter 继续...")
        print()

        cookie_file = "xhs_cookie.txt"
        existing_cookies = ""
        if os.path.exists(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as f:
                existing_cookies = f.read().strip()

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=False)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
            )

            if existing_cookies:
                cks = []
                for part in existing_cookies.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cks.append({
                            "name": k.strip(),
                            "value": v.strip(),
                            "domain": ".xiaohongshu.com",
                            "path": "/",
                        })
                if cks:
                    context.add_cookies(cks)
                    print(f"  已加载已有 cookie ({len(cks)} 个字段)")

            page = context.new_page()
            page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded")
            print()
            input("  登录完成后按 Enter 继续...")

            cookies = context.cookies("https://www.xiaohongshu.com")
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(cookie_str)

            print(f"\n  ✓ Cookie 已保存到 {cookie_file}")
            print(f"  共 {len(cookies)} 个字段")
            browser.close()

    elif args.command == "search":
        args.headless = not args.headed
        output_dir = os.path.join("output", args.keyword)
        spider = XhsSpider(args, output_dir=output_dir, mode=args.mode)
        try:
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
