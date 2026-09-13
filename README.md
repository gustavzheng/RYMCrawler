# RYM Crawler

把 Rate Your Music（RYM）导出的个人评分 CSV，补全为带专辑信息、曲目和封面的本地数据集。

程序默认使用你日常登录的 Edge 打开页面，由 SingleFile 保存网页，再在本地解析。它不会绕过验证码；请控制抓取频率，并遵守 RYM 的使用条款。

## 安装

需要 Windows、Python 3.11+、[uv](https://docs.astral.sh/uv/) 和 Microsoft Edge。

```powershell
git clone <仓库地址>
cd RYMCrawler
uv sync
```

然后：

1. 在 Edge 安装 [SingleFile](https://microsoftedge.microsoft.com/addons/detail/efnbkdcfmcmnhlkaijjjmhjjgladedno)，保存格式选择单文件 HTML，并为新标签页启用自动保存。
2. 打开 `edge://extensions`，启用“开发人员模式”，选择“加载解压缩的扩展”，加载本仓库的 `edge-tab-companion` 目录。
3. 关闭 SingleFile 的“保存后自动关闭标签页”。配套扩展会在数据成功入库后安全关闭标签页。

## 使用

先从 RYM 导出 CSV，再初始化任务：

```powershell
uv run rym_crawler.py prepare "你的导出文件.csv"
```

首次只处理一张专辑：

```powershell
uv run rym_crawler.py fetch --allow-network --limit 1
```

确认 `data/enriched.csv` 正常后，可以增大 `--limit`。默认每次访问间隔 60–120 秒；中断后重复同一命令即可续跑。

如果 SingleFile 不保存到系统的 Downloads 目录：

```powershell
uv run rym_crawler.py fetch --allow-network --limit 20 --inbox "D:\你的\下载目录"
```

常用维护命令：

```powershell
# 修正程序猜错的专辑地址
uv run rym_crawler.py set-url 123456 "https://rateyourmusic.com/release/album/artist/title/"

# 从数据库重新生成 CSV/JSON
uv run rym_crawler.py export

# 查看全部选项
uv run rym_crawler.py --help
uv run rym_crawler.py fetch --help
```

所有个人数据、网页缓存、封面和运行状态都在 `data/`，该目录不会提交到 Git。请自行备份它。

## 遇到问题

- 出现验证码或访问限制：程序会停止并保存进度。请在浏览器中处理后再续跑。
- 等不到 HTML：检查 SingleFile 自动保存是否作用于新标签页，以及 `--inbox` 是否正确。
- 专辑不匹配：用 `set-url` 指定准确的 RYM release URL。
- 页面结构变化：保留出错的 HTML，在提交 issue 时说明错误；不要上传含账号信息的原始页面。

开发、架构和测试说明见 [DEVELOPMENT.md](DEVELOPMENT.md)。
