"""
src/common/paths.py - 项目路径统一管理

无论从根目录还是 src/ 下运行脚本，都通过此模块获取绝对路径，
避免硬编码相对路径导致迁移后失效。
"""

import os

# 项目根目录（向上找到包含 src/ 的那层）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def root(*parts):
    """项目根目录下的路径"""
    return os.path.join(_ROOT, *parts)


def config(*parts):
    """config/ 目录"""
    return root("config", *parts)


def data(*parts):
    """data/ 目录（数据库）"""
    return root("data", *parts)


def logs(*parts):
    """logs/ 目录"""
    return root("logs", *parts)


def output(*parts):
    """output/ 目录（爬取结果）"""
    return root("output", *parts)


def profile(*parts):
    """profile/ 目录（浏览器数据）"""
    return root("profile", *parts)


# ============ 常用路径快捷常量 ============

CONFIG_DIR = config()
DATA_DIR = data()
LOGS_DIR = logs()
OUTPUT_DIR = output()
PROFILE_DIR = profile()

# Cookie 文件
BILI_COOKIE = config("bili_cookie.txt")
XHS_COOKIE = config("xhs_cookie.txt")
XHS_COOKIE_ORIGIN = config("xhs_cookie_origin.txt")
DOUYIN_COOKIE = config("douyin_cookie.txt")

# 日志目录
BILI_LOGS = logs("bili")
XHS_LOGS = logs("xhs")
DOUYIN_LOGS = logs("dy")

# 数据库文件
BILI_DB = data("bili_spider.db")
XHS_DB = data("xhs_spider.db")
XHS_TEST_DB = data("xhs_test_tmp.db")
DOUYIN_DB = data("douyin_spider.db")

# 浏览器 profile
XHS_EDGE_PROFILE = profile("xhs_edge_profile")
DOUYIN_EDGE_PROFILE = profile("douyin_edge_profile")
