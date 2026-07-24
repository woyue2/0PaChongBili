# B站作者粉丝数获取说明

## 问题背景

B 站的粉丝数接口（`/x/relation/stat`、`/x/space/acc/info` 等）需要：
1. WBI 签名验证（动态密钥）
2. Cookie 登录状态
3. 可能的 IP 限制

因此直接通过 API 获取粉丝数的成功率较低。

## 解决方案

### 方案 1：使用 bili-api-python 库（推荐）

```bash
pip install bilibili-api-python
```

```python
from bilibili_api import user, sync

# 获取用户信息
u = user.User(uid=488836173)
info = sync(u.get_user_info())
print(f"粉丝数: {info['fans']}")
```

### 方案 2：手动补充粉丝数

编辑 `bili_uploaders` 表或通过脚本：

```python
# 运行补充脚本
python fill_fans.py
```

### 方案 3：使用浏览器扩展（最稳定）

在 B 站个人空间页面，使用浏览器开发者工具：
1. F12 → Console
2. 执行以下代码复制粉丝数：

```javascript
// 在用户空间页面执行
document.querySelector('.bili-space-avg--fans')?.textContent
// 或
JSON.parse(document.querySelector('script[type="application/json"]')?.textContent || '{}')
```

### 方案 4：使用 Selenium/Playwright 自动获取

```python
from playwright.sync_api import sync_playwright

def get_fans_count(uid, cookie_string):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        
        # 添加 Cookie
        for cookie in cookie_string.split(';'):
            name, value = cookie.strip().split('=', 1)
            context.add_cookies([{
                'name': name,
                'value': value,
                'domain': '.bilibili.com',
                'path': '/'
            }])
        
        page = context.new_page()
        page.goto(f'https://space.bilibili.com/{uid}')
        page.wait_for_load_state('networkidle')
        
        # 获取粉丝数
        fans_element = page.query_selector('.bili-space-avg--fans')
        if fans_element:
            fans = fans_element.text_content().replace('粉丝', '').strip()
            return int(fans)
        
        browser.close()
        return None
```

## 当前代码实现

### 自动获取逻辑

```python
def _fetch_uploader_fans(self, uid, headers, max_retries=2):
    # 1. 检查数据库缓存（7天内有效）
    cached = self.db.get_cached_fans(uid)
    if cached:
        return cached
    
    # 2. 尝试通过 space 页面抓取
    try:
        url = f"https://space.bilibili.com/{uid}"
        resp = requests.get(url, headers=headers, verify=False)
        if resp.status_code == 200:
            match = re.search(r'"fans":(\d+)', resp.text)
            if match:
                fans = int(match.group(1))
                self.db.update_fans(uid, fans)
                return fans
    except:
        pass
    
    # 3. 失败返回 0（后续可手动补充）
    return 0
```

### 结果处理

当 `uploader_fans = 0` 时：
- `conversion_rate` 计算为 0
- `conversion_score` 为 0
- 视频仅按当前值和动量排序

这不影响动量分析的核心功能，只是无法识别"小UP主黑马"视频。

## 推荐操作流程

1. **首次运行**：使用当前脚本爬取数据
2. **补充粉丝数**：使用浏览器扩展或 bili-api-python 批量获取
3. **更新数据库**：运行 `python fill_fans.py` 更新粉丝数
4. **动量分析**：现在可以识别真正的黑马视频

## 粉丝数 API 参考

| API | 状态 | 说明 |
|-----|------|------|
| `/x/relation/stat` | ❌ 需签名 | 返回粉丝数、关注数 |
| `/x/space/acc/info` | ❌ 需签名 | 返回用户基本信息 |
| `/x/master/query` | ❌ 已废弃 | 旧接口 |
| `space.bilibili.com` | ⚠️ 动态加载 | 需 JS 渲染 |

## 总结

由于 B 站 API 的反爬机制，**粉丝转化率功能需要额外配置**。建议：
1. 先使用基础功能（动量分析、增长率计算）
2. 后续通过 bili-api-python 库或手动补充粉丝数
3. 补充后动量分析会自动使用转化率评分