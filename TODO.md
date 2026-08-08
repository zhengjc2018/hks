# 本地 TODO

## 次日高开选股优化（头脑风暴）

- [x] 方案 C：保留规则硬过滤，机器学习只做 TopN 排序
- [x] 方案 A 落地：纯 numpy 逻辑回归，App 只做 sigmoid 推理（已实现）
- [x] 全量主板块训练并验收（3184 只有效标的，Top10 70.3% vs 规则 56.8%，Top3 33.8% vs 规则 31.1%）
- [x] `gap_model.json` 随 APK / EXE 一起打包（Android copy + EXE --add-data）
- [x] Windows EXE 自动更新（检查 GitHub release / 下载 / 退出后替换重启）
- [ ] 观察池推荐排序复用同一模型（后续再看）

## B 方案存档（scikit-learn 直装 App，后续再看）

背景：原计划把 scikit-learn / LightGBM 直接打进 APK / EXE 用于训练或推理。

打包风险：

1. Android Chaquopy 是 Python 3.11，scikit-learn 没有官方 Android wheel，pip 会尝试源码编译，需要 C 编译器 + OpenBLAS/LAPACK，大概率失败；即使装上，APK 体积和构建时间也会显著增加。
2. Windows PyInstaller 对 sklearn/scipy 这类 C 扩展库需要大量 hidden imports 和动态库处理，onefile 体积容易涨到 1GB 以上，现有 EXE 构建 workflow 也要同步改。
3. 跨端兼容：Windows 训练用 Python 3.12，Android 运行是 Python 3.11，joblib/pickle 模型可能不兼容，需要 ONNX 或权重 JSON 中转，等于绕了一圈。

如果以后要做，优先走「服务端训练 sklearn/LightGBM，App 只做 numpy/ONNX 推理」，不要把 sklearn 装进移动端。
