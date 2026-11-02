"""漫剧故事输入层（Manju Story Input Layer）—— 小说 → v10.2 渲染契约。

本包只实现 L1 故事层 + L2 契约编译；L3 能力注册表 / L4 执行层属完整联调包，
不在本包内。CLI 与 Studio 共用同一套流水线，产物完全一致。

模块划分：
    story_cli   独立命令行入口
    studio      Manju Studio 本地 Web 服务（可视化前端，非第二套流水线）
    story       故事层：ingest / episodes / plot / summarize / stages /
                validate / compile / review / continuity / clean 等
    schemas     Pydantic 数据模型与 JSON 解析（唯一契约来源）
    llm         OpenAI 兼容（DeepSeek / 豆包·火山方舟）客户端 + JSON 强约束 + 修复重试
    gate        v10.2 跨文档契约审计
    drama       戏剧结构 / 分镜审计
    config      配置加载（config.json + 环境变量覆盖）
    tracing     OpenTelemetry / OpenInference 埋点（缺依赖自动降级为空操作）
    report      故事层报告
"""

__version__ = "2.0.0"
