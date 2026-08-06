# A股机会雷达

> 内部群友分发，**不公开、不转卖**。本程序仅供学习研究，不构成任何投资建议。

## 一、本地运行

需要 Python 3.12+，首次运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
cp config.example.json config.json
```

macOS 可双击 `start.command`（找不到空闲端口时会自动换 5050/5001 等），
或直接启动：

```bash
APANEL_LLM_MOCK=1 .venv/bin/python server.py
```

浏览器打开 `http://127.0.0.1:5000/`。要停就结束 `server.py` 进程。

## 二、云部署

项目已内置 `Dockerfile` 和 `wsgi.py`，使用 gunicorn 启动，默认监听
`0.0.0.0:8000`，可用任意 PaaS / VPS + Docker 部署：

```bash
docker build -t hks .
docker run -p 8000:8000 hks
```

云容器建议把以下运行期文件挂到持久卷，避免重启丢数据：
`config.json`、`positions.json`、`holdings.json`、`lifecycle.json`、
`strategic_overlay.json`、`watched_boards.json`、`_*_cache.json`。

注意：后台调度器只在单个 gunicorn worker 内启动，`Dockerfile` 已固定
`--workers 1`，不要自行调大 worker 数。

## 三、需要联网的地方（仅行情/数据）

- 行情数据：程序直连通达信公共行情服务器（无需本机装通达信、无需账号）。
  连不上时自动回退新浪/腾讯/东方财富（均公开免费）。板块矩阵主要走通达信，需能稳定连通达信行情服务器，否则核心功能打折。
- 新闻公告：巨潮网（证监会旗下公开源，经通达信通道，无账号）。

## 四、关于「战略叠加 / 大模型分析」

- **默认离线兜底**：内置 LLM 网关已失效、群友大多无 key，所以启动脚本设了 `APANEL_LLM_MOCK=1`——板块简评/总览走离线模拟，界面照样有内容（标注为模拟）。
- 想开**真实**战略分析：在设置面板填入你**自己的**大模型 endpoint / key，`strategic_overlay_enabled` 自动变 true，同时把启动脚本里的 `APANEL_LLM_MOCK=1` 删掉或改成 `0`。
- **作者密钥绝不随包分发**，请用自己的。

## 五、仓位披露说明

展示仓位提示为中文 **轻 / 小 / 中 / 重 / 轻减 / 中减**，根据个人投资习惯进行跟仓。

## 六、问题反馈（上报问题按钮）

- 界面右上角有 **📝 上报问题** 按钮，点一下会：①自动采集诊断信息（版本号、操作系统、最近一次报错、时间戳）并复制到剪贴板；②自动打开问题反馈收集表。
- 收集表提交链接：`https://docs.qq.com/form/page/DY3ZIc1RWR0tjTWxJ`（所有人可填，数据直达作者，**不经任何中转**）。
- 群友只需把剪贴板内容粘贴进表单描述框即可，作者收到的是带上下文的工单，不用反过来追问"你用的哪个版本 / 报的什么错"。
- 想换收集表：改 `config.json` 里的 `issue_form_url` 即可，链接是配置项。

## 七、目录结构

```
hks/
  server.py              # 主程序入口
  wsgi.py                # gunicorn 入口（云部署）
  Dockerfile             # 容器镜像
  start.command          # macOS 本地启动器
  *.py                   # 后端模块
  frontend/              # 前端静态资源
  config.example.json    # 配置模板（首次启动复制为 config.json）
  requirements.lock.txt  # 依赖精确版本
```

## 八、已知限制

- 当前 LLM 网关失效期间，真实大模型分析需群友自备 endpoint+key；否则看到的是离线模拟简评。
- 网络质量差时，板块矩阵冷加载可能偏慢（启动已预热，正常使用不卡）。
