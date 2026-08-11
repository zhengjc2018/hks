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

**Render 免费部署（推荐）**：仓库里已带 `render.yaml`，把
`https://github.com/zhengjc2018/hks` 导入 Render 后会自动识别并部署。
部署完成后会得到一个 `https://hks.onrender.com/` 形式的地址，把这个地址填进
安卓 App 的后端地址即可，手机不需要再依赖本地电脑。

## 三、安卓 App

安卓 App 使用 Chaquopy 把 Python 后端直接打进 APK，手机安装后不需要云服务、
也不依赖本地电脑；打开 App 即在手机内部启动 Flask 并显示网页版界面。

- 安装包在 GitHub Release 中（打 `v*` tag 后由 GitHub Actions 自动构建发布），
  也可从 Actions 的 `hks-apk` 工件下载。
- 行情数据仍需联网获取（通达信 / 新浪 / 东方财富），但不需要任何你自己的云服务。
- 首次启动会在手机内部存储生成配置文件；后台扫描和调度也在 App 内运行。
- 安装包内置了当前网页版的 `lifecycle.json` / `positions.json` / `holdings.json`
  初始数据快照，首次打开即可看到与网页版一致的生命周期数据。
- 安卓源码在 `android/`，云端构建由 `.github/workflows/build-android.yml` 自动完成。

## 四、需要联网的地方（仅行情/数据）

- 行情数据：程序直连通达信公共行情服务器（无需本机装通达信、无需账号）。
  连不上时自动回退新浪/腾讯/东方财富（均公开免费）。板块矩阵主要走通达信，需能稳定连通达信行情服务器，否则核心功能打折。
- 新闻公告：巨潮网（证监会旗下公开源，经通达信通道，无账号）。

## 五、关于「战略叠加 / 大模型分析」

- **默认离线兜底**：内置 LLM 网关已失效、群友大多无 key，所以启动脚本设了 `APANEL_LLM_MOCK=1`——板块简评/总览走离线模拟，界面照样有内容（标注为模拟）。
- 想开**真实**战略分析：在设置面板填入你**自己的**大模型 endpoint / key，`strategic_overlay_enabled` 自动变 true，同时把启动脚本里的 `APANEL_LLM_MOCK=1` 删掉或改成 `0`。
- **作者密钥绝不随包分发**，请用自己的。

## 六、仓位披露说明

展示仓位提示为中文 **轻 / 小 / 中 / 重 / 轻减 / 中减**，根据个人投资习惯进行跟仓。

## 七、问题反馈（上报问题按钮）

- 界面右上角有 **📝 上报问题** 按钮，点一下会：①自动采集诊断信息（版本号、操作系统、最近一次报错、时间戳）并复制到剪贴板；②自动打开问题反馈收集表。
- 收集表提交链接：`https://docs.qq.com/form/page/DY3ZIc1RWR0tjTWxJ`（所有人可填，数据直达作者，**不经任何中转**）。
- 群友只需把剪贴板内容粘贴进表单描述框即可，作者收到的是带上下文的工单，不用反过来追问"你用的哪个版本 / 报的什么错"。
- 想换收集表：改 `config.json` 里的 `issue_form_url` 即可，链接是配置项。

## 八、目录结构

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

## 九、次日高开排序模型（可选）

`gap_model.json` 会随 APK / EXE 一起打包，次日高开候选保留全部规则硬过滤，
只由模型调整 TopN 排序。模型在桌面端离线训练，手机 / 电脑打包版不会自动训练：

```bash
# 冒烟：几只票验证管线
.venv/bin/python train_gap_model.py --codes 600519,000001 --epochs 30

# 全主板块训练（不依赖 sklearn，产物 gap_model.json 会随包分发）
.venv/bin/python train_gap_model.py --industry-filter --epochs 100

# 只训练热点板块成分股：把代码列表写入文件后
.venv/bin/python train_gap_model.py --symbols-file universe.txt --epochs 100
```

训练脚本会输出测试集上模型 Top1/3/10 与规则基线的对比，并把逐日候选写入
`backtest_report/gap_model_train_report.csv`。训练完成后重启服务即自动加载
`gap_model.json`，前端候选表会从“得分”切换成模型概率；删掉模型文件即回到
规则排序。重新训练后记得把新的 `gap_model.json` 一起提交，下一次 APK / EXE
构建会自动带上。

## 十、Windows 自动更新（仅打包版）

顶部「检查更新」按钮会读取 GitHub release（默认 `zhengjc2018/hks` 的
`v1.0.0-windows` tag），发现新版本后下载对应 exe 到本地数据目录，用户确认后
启动更新器：程序退出 → 替换 `hks.exe` → 自动重新打开。源码运行或非 Windows
环境只提示不支持，不会误装。发布新版本时记得同时更新 `app_update.py` 里的
`APP_VERSION`（或 `config.json` 的 `app_version`）。

## 十一、数据增强（a-stock-data）

移植了 a-stock-data 的高价值数据端点：

- 首页「市场温度」：打板情绪（涨停/炸板/跌停/炸板率/连板梯队/昨涨停晋级率）、
  板块主力资金流、同花顺人气榜、财联社/全球快讯。
- 个股详情页「数据面」：龙虎榜、两融、大宗交易、股东户数、分红、120日资金流、
  研报、互动易、个股新闻、概念热度。
- 行情兜底：通达信失败后除新浪外增加百度日 K 兜底；腾讯行情封装可用于估值字段。

东财接口统一走限流，失败返回空并在前端显示“暂无数据”，不影响主流程。

## 十二、已知限制

- 当前 LLM 网关失效期间，真实大模型分析需群友自备 endpoint+key；否则看到的是离线模拟简评。
- 网络质量差时，板块矩阵冷加载可能偏慢（启动已预热，正常使用不卡）。
