# 开发指南

本文给维护者和编码 Agent 使用。用户安装步骤只放在 `README.md`。

## 快速验证

```powershell
uv sync
uv run python -m unittest discover -s tests -v
node --test tests/tab_companion.test.cjs
uv run rym_crawler.py --help
```

测试默认离线运行。不要让测试访问 RYM，也不要把真实导出、网页、数据库或封面加入 fixture。

## 结构

| 路径 | 职责 |
| --- | --- |
| rym_crawler/__main__.py | 唯一公开入口：python -m rym_crawler |
| rym_crawler/cli.py | 参数解析、SQLite 状态、HTML 解析、导入和导出 |
| rym_crawler/release_urls.py | 候选 URL、转写和页面身份核对 |
| rym_crawler/cover_cache.py | 从保存页面提取并校验封面 |
| rym_crawler/tab_bridge.py | Python 与 Edge 配套扩展之间的本地握手 |
| rym_crawler/manual_match.py | 高级批量匹配维护工具 |
| rym_crawler/transports/ | SingleFile（默认）、Playwright 和手动 Edge 传输层 |
| edge-tab-companion/ | 只关闭本工具登记且已成功处理的标签页 |
| tests/ | Python 单元测试与扩展的 Node 测试 |

`data/` 是唯一的默认运行目录。数据库是事实来源，`enriched.csv` 和 `enriched.json` 都可以用 `export` 重建。

## 不变量

- 网络访问必须由 `fetch --allow-network` 显式开启；`prepare`、`reparse`、`import-html` 和 `export` 必须离线。
- 只接受 `https://rateyourmusic.com/release/...` 地址，任何重定向也必须再次校验。
- 页面身份匹配失败时不得写成 `done`，也不得误关无关标签页。
- 每条记录先原子写入缓存并提交 SQLite，再导出结果、通知扩展关闭标签页。
- 遇到验证码、限流、未知页面结构或关闭确认失败时停止队列并保留进度。
- 新增输出字段时同时更新 JSON/CSV 导出和覆盖对应测试。

## 修改流程

1. 先定位最小职责模块；不要在传输层复制解析或数据库逻辑。
2. 用合成的最小 HTML/CSV 写回归测试，禁止提交真实用户页面。
3. 运行全部 Python 和扩展测试。
4. 检查 `git status --ignored`，确认导出、缓存、密钥和本机路径未进入提交。

依赖只维护在 `pyproject.toml`，锁文件由 `uv lock` 更新。项目目前采用直接脚本入口，不要求安装为 Python 包。

## 发布前检查

```powershell
git status --short
git ls-files | rg "(^data/|\.csv$|\.html?$|\.sqlite3|\.env$|manual-downloads)"
rg -n -i "(api[_-]?key|secret|password|authorization|bearer|sessionid)" -g "!.git/**" .
```

敏感信息检查可能命中测试变量或本地握手 token，应人工判断。永远不要上传真实 cookie、Authorization header、账号页面或个人导出。
