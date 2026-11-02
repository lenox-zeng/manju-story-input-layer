"""配置加载：config.json 为唯一来源，代码内含默认值兜底。

设计要点
--------
* 任何键缺失都不会导致崩溃 —— 逐层深度合并到内置默认值上。
* 支持环境变量覆盖少数运行时开关（总线模式、遥测端点等），
  便于不改文件就能切换「本地/分布式」「遥测开/关」。
* 路径统一保留字符串形式（Windows 下 pathlib 与 httpx 都能吃）；
  需要路径对象时用 `Path(cfg.path(...))`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence

# --------------------------------------------------------------------------
# 内置默认值：即使 config.json 丢了也能给出可诊断的错误而不是 AttributeError
# --------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    # ----------------------------------------------------------------------
    # 遗留配置节说明（v2.0 输入层）：
    #   - observability.*  ：输入层 tracing 实际读取（默认关闭），保留；
    #   - comfy / bus / dashboard_port / pipeline.* / anim2d / hero_video /
    #     char_views       ：旧完整联调包（L3/L4）遗留配置，输入层**不消费**，
    #                        保留仅为未来对接与兼容；story_cli --doctor
    #                        会提示其中 3 个死键（pipeline.llm_mode /
    #                        contract_gate / drama_gate）。
    #   - 任何键缺失都不会导致崩溃：逐层深度合并到内置默认值上。
    # ----------------------------------------------------------------------
    # 以下均为**可移植占位值**：实际机器路径请写进 config.json。
    # `MANJU_ROOT` 环境变量可覆盖项目根目录。
    "project_root": ".",
    "python": r".venv/Scripts/python.exe",
    "ffmpeg": "ffmpeg",
    "comfy": {
        "url": "http://127.0.0.1:8188",
        "install_dir": "",
        "python": "",
        "base_directory": "",
        "input_dir": "",
        "output_dir": "",
        "free_timeout_sec": 90,
        "startup_timeout_sec": 300,
        "launch_args": ["--listen", "127.0.0.1", "--port", "8188"],
        "log": "logs/comfyui.log",
        "prompt_timeout_sec": 3600,
        "poll_interval_sec": 2.0,
    },
    "llm": {
        # ==== 文本模型（OpenAI 兼容 API）====
        # 当前后端：DeepSeek（OpenAI 兼容，OpenAI SDK 直接对接）。
        # - base_url = https://api.deepseek.com/v1
        # - model = deepseek-v4-pro（更强） / deepseek-flash（更快，默认）
        # 密钥优先读环境变量 MANJU_LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY；
        # config.json 的 llm.api_key 仅作旧配置兼容，--doctor 会提示迁移。
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "api_key": "",
        # 内部兜底：各生成阶段都显式传温度（机械阶段 0.0，卷摘要 0.2）。
        # 未显式传值时也默认 0.0，避免旧配置/新调用悄悄引入随机性。
        "temperature": 0.0,
        "max_tokens": 6000,
        "json_repairs": 2,
        # 网络层重试：仅对超时/限流/5xx 生效；指数退避，单次上限 30s
        "api_retries": 2,
        "api_retry_backoff_sec": 2.0,
        "request_timeout_sec": 900,
        # A：思考模式。auto=不传参数；disabled=关思考；low/medium/high=推理强度。
        # 机械阶段（EDL/站位/提示词展开）关掉或降档，创意阶段（圣经/剧本）保持高档。
        # 端点不支持时自动降级重试，不会因为一个参数整批失败。
        "thinking": "low",
        "thinking_by_stage": {},
        # I：流式输出 + 空闲看门狗。read=180s 表示 180s 内没有任何 chunk 才断开，
        # 不会把正常的长思考误杀；总时长仍受 request_timeout_sec 约束。
        "stream": True,
        "stream_idle_timeout_sec": 180,
        # B2：剧本/站位跨集并行生成的最大并发数（远程 API 天然支持并发）
        "parallel": 3,
        # B：分镜提示词展开的跨场并发数（纯读，不依赖其他场次）
        "prompt_parallel": 4,
        # 长篇压缩 / 自动分集（见 manju/story/summarize.py、episodes.py、plot.py）
        # 30k 以内不再压缩：压缩是有损的，18k 的小说压成 700 字会直接丢掉因果链。
        "story_source_chars": 30000,
        "story_chunk_chars": 5000,
        # 仅在显式 --episodes N / 关闭情节模式时作为字数分集兜底。
        "story_chars_per_episode": 9000,
        # 分集：默认情节模式（LLM 按戏剧单元分集）。
        # >0 时强制“每 N 章一集”（作者章节优先）；0=不按章节。
        "story_chapters_per_episode": 0,
        "story_plot_mode": True,
        # 情节原子：每段约 2400 字；给 LLM 的摘录 900 字；长篇按总预算均摊。
        "story_plot_atom_chars": 2400,
        "story_plot_atom_excerpt": 900,
        "story_plot_max_atoms": 180,
        "story_plot_total_chars": 100000,
        # 只生成前 N 集（--to-episode）时，设计阶段只读前 N×该字数：
        # 长篇不必为未生成的后文付 token，也能让前几集看到更完整的细节。
        "story_plot_prefix_chars_per_episode": 5000,
        # 情节模式下按批写分集梗概：每批 3 集，让每集的整段原文（≤20k）
        # 都能喂进去；批太大时中段会被摘录截掉，因果细节随之丢失。
        "story_plot_episode_batch": 3,
        # 长篇压缩时每卷章节数：卷越小，因果细节保留越多。
        "story_volume_chapters": 5,
        "story_fallback_chunk": 20000,
        "story_fallback_overlap": 500,
        "story_max_volumes": 60,
        "story_episode_batch": 10,
        "story_max_auto_episodes": 12,
    },
    # 故事/台词节奏与引擎能力参数（story.*）。旧 pipeline.* 仅作兼容。
    # story.engine：""（通用默认）/ "comfyui" / "seedance" / "jimeng"。
    # 选定引擎后，未显式配置的字段（max_shot_seconds / max_faces_per_shot /
    # max_line_chars / camera_motion 等）取 engines.py 档案推荐值；显式配置优先。
    "story": {
        "prompt_language": "en",
        "engine": "",
    },
    "pipeline": {
        "max_qa_rounds": 3,
        "width": 832,
        "height": 1472,
        "shots_per_scene": 1,
        "frames_vision_sample": 6,
        "keep_comfy_output_copy": True,
        "qa_required": False,
        # 契约 §10.1 跨对象门：report=只报告不阻断（默认）；strict=有 block 就中止；
        # off=不跑。首集建议先 report，看清结论再决定要不要卡。
        "contract_gate": "report",
        # v12 戏剧/分镜门：与 gate 分开。report=只报告；strict=有 block 就中止出图；
        # off=不跑。没有 director.json 时只 warn（旧集兼容）。
        "drama_gate": "report",
        "episode_sec": 90.0,
        # 角色一致性资产：identity + 6 视角 + 7 表情 + 服装板。
        # 代码自带同一套默认值；config.json 缺省时保持关闭（不悄悄跑图）。
        "char_views": {
            "enabled": False,
            "max_refs": 4,
            "wardrobe_assets": True,
            "face_qa": {
                "enabled": False,
                "model": "dino",
                "model_dir": "",
                "threshold": None,
                "strict": False,
            },
        },
        # 文本来源：local=本流水线调用 LLM API（DeepSeek / 豆包 / 任意兼容端点）生成策划/编剧/分镜；
        # external=用豆包等外部工具生成后放入 input/external/，流水线只做出图/成片等环节。
        "llm_mode": "local",
        # 视频后端：ltx=逐镜 I2V；2.5d=首帧图有限动画（推荐）；hybrid=2.5D + 云英雄镜头。
        "av_mode": "ltx",
        "anim2d": {
            "width": 1080,
            "height": 1920,
            "fps": 24.0,
            "overscan": 1.16,
            "breath": 0.0045,
            "handheld": 0.0035,
            "action_shake": 0.0035,
            "crf": 18,
            "preset": "veryfast",
            "transition_sec": 0.0,
            "reuse": True,
            "vignette": 0.0,
            "coverage": {
                "enabled": True,
                "min_shot_sec": 3.5,
                "three_panel_sec": 6.5,
                "panels_2": [[1.00, 0.000, 0.000], [1.13, 0.000, -0.015]],
                "panels_3": [[1.00, 0.000, 0.000], [1.10, 0.000, -0.010],
                             [1.22, 0.040, 0.000]],
            },
        },
        "hero_video": {
            "enabled": False,
            "provider": "mock",
            "shots": [],
            "max_sec": 5.0,
            "fallback_2d": True,
            "reuse": True,
            "provider_options": {},
        },
    },
    "bus": {
        "mode": "local",
        "coordinator_port": 9000,
        "employee_ports": {
            "planner": 9001, "writer": 9002, "storyboard": 9003,
            "renderer": 9004, "av": 9005, "qa": 9006, "delivery": 9007,
        },
    },
    "observability": {
        "enabled": False,
        "otlp_endpoint": "http://127.0.0.1:4318/v1/traces",
        "phoenix_endpoint": "http://127.0.0.1:6006/v1/traces",
        "project": "manju-pipeline",
    },
    "dashboard_port": 8090,
}

# 明文密钥不再建议写进 config.json。按优先级读取环境变量，最后才回落到
# 旧配置，保证老用户升级后仍能启动，同时给 --doctor 留出迁移提示。
API_KEY_ENVS = ("MANJU_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


def env_api_key() -> str:
    """返回环境变量里的 API Key；没有则空串（绝不打印内容）。"""
    for name in API_KEY_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def api_key_source(cfg: "Config | None" = None) -> str:
    """密钥来源标签，供 --doctor / Studio 展示；不返回密钥本身。"""
    for name in API_KEY_ENVS:
        if os.environ.get(name):
            return f"env:{name}"
    if cfg is not None and str(cfg.get("llm.api_key", "") or ""):
        return "config.json（建议迁移到环境变量）"
    return "missing"


def api_key_value(cfg: "Config | None" = None) -> str:
    """按环境变量 > 旧 config 的顺序取密钥；调用方不要记录返回值。"""
    return env_api_key() or (str(cfg.get("llm.api_key", "") or "") if cfg else "")


class Config:
    """只读配置视图，支持 `cfg["comfy"]["url"]` 与 `cfg.get("a.b.c", default)`。"""

    def __init__(self, data: dict[str, Any], source: Path | None = None):
        self._data = data
        self.source = source

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        env_root = os.environ.get("MANJU_ROOT")
        root = Path(env_root) if env_root else Path(DEFAULTS["project_root"])
        p = Path(path) if path else root / "config.json"
        user: dict[str, Any] = {}
        if p.is_file():
            try:
                user = json.loads(p.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as e:  # 配置坏了要早失败且说清楚
                raise SystemExit(f"config.json 解析失败：{p}\n  {e}") from e
        data = _deep_merge(json.loads(json.dumps(DEFAULTS)), user)
        # 显式 MANJU_ROOT 优先于配置文件；否则用配置里的 project_root。
        data["project_root"] = str(env_root or data.get("project_root") or root)
        cfg = cls(data, p if p.is_file() else None)
        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        """环境变量覆盖：不改文件即可切模式。"""
        env = os.environ
        if env.get("MANJU_BUS_MODE"):
            self._data["bus"]["mode"] = env["MANJU_BUS_MODE"]
        obs = self._data["observability"]
        if env.get("PHOENIX_ENABLED") is not None:
            obs["enabled"] = env["PHOENIX_ENABLED"] not in ("0", "", "false", "False")
        if env.get("OTEL_TRACES_ENDPOINT"):
            obs["otlp_endpoint"] = env["OTEL_TRACES_ENDPOINT"]
        if env.get("MANJU_PHOENIX_ENDPOINT"):
            obs["phoenix_endpoint"] = env["MANJU_PHOENIX_ENDPOINT"]
        if env.get("MANJU_COMFY_URL"):
            self._data["comfy"]["url"] = env["MANJU_COMFY_URL"]

    # -- 读取 -------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self._data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    # -- 路径便捷方法 ------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self._data["project_root"])

    def p(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def ensure_dirs(self) -> None:
        for sub in ("logs", "state", "output", "tools", "manju", "observability"):
            self.p(sub).mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        src = str(self.source) if self.source else "<内置默认值>"
        return (
            f"配置来源: {src}\n"
            f"  项目根: {self.root}\n"
            f"  总线模式: {self.get('bus.mode')}\n"
            f"  LLM:  {self.get('llm.provider')}  {self.get('llm.base_url')}  model={self.get('llm.model')}\n"
            f"  comfy:  {self.get('comfy.url')}\n"
            f"  遥测:   {'开' if self.get('observability.enabled') else '关'} "
            f"-> {self.get('observability.otlp_endpoint')}"
        )


# --------------------------------------------------------------------------
# 命令行覆盖：`--set pipeline.object_ref.weight=0.35`
#
# 为什么要有它：这套流水线里有一批**没有实测依据的默认值**（4 视角一起喂 IPAdapter
# 会不会把身份稀释掉、物件链权重 0.35 合不合适），只能靠"同一镜、同一种子、改一个
# 参数跑两次"来定标。没有覆盖就只能去改 config.json —— 改了容易忘、忘了就再也说不清
# 那次跑用的是什么参数。覆盖会被记进跑批台账（`run.overrides`），A/B 两次跑完能对着
# 台账看出差异，这是"未验证的假设"变成"有数据的结论"的唯一诚实路径。
# --------------------------------------------------------------------------
_MISSING = object()


def parse_override(text: str) -> tuple[str, Any]:
    """`"a.b.c=VALUE"` → `("a.b.c", value)`。

    VALUE 先按 JSON 解析，所以 `0.35` 是浮点、`false` 是布尔、`[1,2]` 是列表；
    解析失败则**原样当字符串** —— 提示词这类值自带引号和逗号，不该逼用户转义。
    """
    if "=" not in text:
        raise ValueError(f"{text!r} 不是 KEY=VALUE 形式（缺 `=`）")
    key, _, raw = text.partition("=")
    key = key.strip()
    if not key:
        raise ValueError(f"{text!r} 的键是空的")
    if not all(p.isidentifier() for p in key.split(".")):
        raise ValueError(f"键 {key!r} 里有非法字符（只支持 a.b.c 这种点分路径）")
    raw = raw.strip()
    try:
        return key, json.loads(raw)
    except json.JSONDecodeError:
        return key, raw


def apply_overrides(cfg: "Config", overrides: Sequence[str]) -> tuple[list[str], list[str]]:
    """把覆盖应用到 `cfg` 上，返回 `(生效的 k=v 列表, 警告列表)`。

    "本来存在的键"和"新键"分开报：`--set pipeline.object_ref.weigth=0.5` 这种拼写错误
    会**静默新建一个没人读的键**，跑完和没跑一模一样 —— 正是这套流水线最怕的失败模式。
    新键照设（config.json 里本来就允许某些键缺席），但一定警告。

    生效的覆盖会写进 `run.overrides`，跑批台账会把它记下来。
    """
    # **先全部解析，再落地**：半途生效（前一项改了、后一项报错）会留下一个
    # "改了一半"的配置，而那种状态没人能复现 —— 不如一项都不改。
    parsed = [parse_override(item) for item in overrides]
    applied: list[str] = []
    warns: list[str] = []
    for key, value in parsed:
        if cfg.get(key, _MISSING) is _MISSING:
            warns.append(f"{key} 在 config.json 里本来不存在 —— 先确认拼写；"
                         f"若确实是新键，很可能没有任何代码读它")
        cfg.set(key, value)
        applied.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    if applied:
        cfg.set("run.overrides", applied)
    return applied, warns


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


_CACHED: Config | None = None


def get_config(reload: bool = False, path: str | None = None) -> Config:
    """进程内单例，避免每个模块各读一遍磁盘。"""
    global _CACHED
    if _CACHED is None or reload:
        _CACHED = Config.load(path)
    return _CACHED
