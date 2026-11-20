# 给A子上香 · V2.0 在线试玩

本目录包含《给A子上香》V2.0「A股灵堂风」独立试玩入口，公开部署在 GitHub 的 `demo/gei-a-zi-shang-xiang-v2` 分支，未修改原始项目主干。

## 在线预览
`https://rawcdn.githack.com/zhengjc2018/hks/7f9eca07038e3a6f09debb1b6c690ac525f948fc/gei-a-zi/v2/index.html`

## 源码说明
为了在单次 GitHub 提交中发布完整 H5，`index.html` 是可运行的 gzip 入口包装器，内嵌了完整原始 HTML。浏览器通过 `DecompressionStream('gzip')` 还原后运行，没有请求后端。最新版 Chrome / Edge / Safari 支持。
开发时可用下列命令从包装器提取完整单文件原始源码：

```python
from pathlib import Path
import base64, gzip, re
wrapper = Path('index.html').read_text()
payload = re.search(r'const data="([A-Za-z0-9+/=]+)"', wrapper).group(1)
Path('original-index.html').write_bytes(gzip.decompress(base64.b64decode(payload)))
```

完整游戏特性：暗红祭台、疲惫的 A 子形象、点击上香、动态烟雾与吐槽、每日求签、分享、里程碑与 V1 本地存档兼容。所有台词为娱乐，不提供投资建议。

此分支不是 TapTap 审核通过证明。用户可在完整原版 `index.html` 上继续开发，不建议以此包装器作为 TapTap 最终包体。
