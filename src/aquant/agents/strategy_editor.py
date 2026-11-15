"""StrategyEditorAgent:LLM 生成策略 → Layer 1 校验 → 带 findings 重试。

落地"校验-重试-防作弊"三件套(借鉴 PA_Agent,见 docs/design.md 1.2):
- 生成 → 提取 python 代码块 → validate_strategy(仅代码,无 run_config);
- 有 ERROR 时把 findings 文本化拼进下一轮 user 消息重试,最多 3 次;
- 重试 prompt 中带防作弊声明:只允许修复指出的问题,不得删除或绕过校验逻辑;
- 3 次后仍有 ERROR → 抛出 StrategyGenerationError(携带最终 Verdict 与最后一次代码)。

经验库注入(复盘-反思-优化闭环,docs/design.md §1.2):注入可选 ExperienceStore 时,
generate 前先按需求特征标签检索 top-k 相似历史案例,以固定段落拼入 user 消息
(仅参考,禁止照抄);系统提示词与重试/防作弊语义保持不变。
"""

from __future__ import annotations

import re
import warnings
from typing import Callable

from aquant.agents.experience import ExperienceStore, derive_tags
from aquant.agents.experience.store import MAX_CODE_LINES
from aquant.agents.llm import LLMClient
from aquant.agents.prompts import render_prompt
from aquant.validation.findings import Finding, RuleId, Severity, Verdict
from aquant.validation.validator import validate_strategy

MAX_ATTEMPTS = 3
CASES_TOP_K = 3

# 案例段落标题(逐字写入 user 消息,测试会断言该文案)
CASES_HEADING = "## 相似历史案例(供参考,禁止照抄)"

# 允许 ```python / ```py 等围栏;DOTALL 跨行匹配
_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.DOTALL)

# 重试时的防作弊声明(逐字写入 user 消息,测试会断言该文案)
ANTI_CHEAT_CLAUSE = (
    "防作弊约束:只允许修复上述指出的问题,不得删除或绕过校验逻辑;"
    "不得通过删除功能或注释代码来消除告警;上一轮已通过校验的部分保持不变。"
)


class StrategyGenerationError(RuntimeError):
    """连续 MAX_ATTEMPTS 次生成后仍未通过 Layer 1 校验。"""

    def __init__(self, verdict: Verdict, attempts: int, last_code: str | None = None) -> None:
        self.verdict = verdict
        self.attempts = attempts
        # 最后一次成功提取出的代码("最佳尝试",供经验库记录失败案例;可能为 None)
        self.last_code = last_code
        super().__init__(
            f"策略生成失败:连续 {attempts} 次未通过 Layer 1 校验:\n"
            + "\n".join(verdict.error_messages())
        )


def extract_python_block(text: str) -> str | None:
    """从 LLM 输出中提取 python 代码块。

    优先取围栏代码块;无围栏但文本本身像策略代码时整体返回(兜底);
    两者皆不满足返回 None。
    """
    match = _CODE_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if "def init" in stripped and "def handle_bar" in stripped:
        return stripped
    return None


def _format_feedback(verdict: Verdict, attempt: int) -> str:
    """把上一轮的 ERROR findings 文本化,拼装第 attempt+1 轮的重试反馈。"""
    lines = [
        f"## 第 {attempt + 1} 次生成反馈:上一轮代码未通过平台 Layer 1 校验",
        "以下 ERROR 必须全部修复:",
    ]
    lines.extend(f"- {msg}" for msg in verdict.error_messages())
    lines.append(ANTI_CHEAT_CLAUSE)
    return "\n".join(lines)


def _format_cases(cases: list[dict]) -> str:
    """把经验库检索到的 top-k 案例文本化为段落(每条代码截断至 MAX_CODE_LINES 行)。"""
    lines = [
        CASES_HEADING,
        "以下为经验库中按需求特征检索到的历史案例,仅供开阔思路与避开已知坑;",
        "禁止照抄:历史代码未必满足本轮需求,直接复用无法通过评审与原创性检查。",
    ]
    for i, case in enumerate(cases, 1):
        status = "成功" if case.get("passed") else "失败"
        tags = ", ".join(str(t) for t in (case.get("tags") or [])) or "无"
        lines.append(f"\n### 案例 {i}({status})特征标签:{tags}")
        requirement = str(case.get("requirement") or "").strip()
        if requirement:
            lines.append(f"需求:{requirement}")
        code = str(case.get("code") or "").strip()
        if code:
            all_lines = code.splitlines()
            shown = "\n".join(all_lines[:MAX_CODE_LINES])
            lines.append("参考代码:")
            lines.append("```python")
            lines.append(shown)
            lines.append("```")
            if len(all_lines) > MAX_CODE_LINES:
                lines.append(f"(代码过长,仅展示前 {MAX_CODE_LINES} 行,共 {len(all_lines)} 行)")
        else:
            lines.append("参考代码:无(该案例未产出有效代码)。")
    return "\n".join(lines)


class StrategyEditorAgent:
    """策略编辑子 agent:系统提示词用硬约束模板,user 消息承载需求与重试反馈。"""

    def __init__(
        self,
        llm: LLMClient,
        validator: Callable[[str], Verdict] = validate_strategy,
        experience: ExperienceStore | None = None,
    ) -> None:
        """llm:LLMClient 协议实现;validator:代码级校验函数(validate_strategy);
        experience:经验库(可选)。提供时按需求特征标签检索 top-k 相似历史案例注入
        user 消息(仅参考、禁止照抄),系统提示词与重试/防作弊语义不变。
        """
        self.llm = llm
        self.validator = validator
        self.experience = experience
        self.system = render_prompt("strategy_editor.md")
        self.attempts = 0  # 最近一次 generate 的实际尝试次数(1..MAX_ATTEMPTS)

    def _search_cases(self, user_text: str) -> str:
        """检索相似历史案例并文本化为段落;无案例或检索异常时返回空串(不阻塞生成)。"""
        if self.experience is None:
            return ""
        try:
            cases = self.experience.search(derive_tags(user_text), top_k=CASES_TOP_K)
        except Exception as exc:  # 经验库检索失败不应影响主生成流程
            warnings.warn(f"经验库检索失败(已忽略,不影响生成):{exc}", stacklevel=2)
            return ""
        if not cases:
            return ""
        return _format_cases(cases)

    def generate(self, user_text: str, runtime_error: str | None = None) -> str:
        """生成通过 Layer 1 校验的策略源码。

        :param runtime_error: 上一轮生成代码在**回测执行阶段**的崩溃文本,由编排器
            的"生成-执行自愈闭环"回馈。静态校验无法发现 API 误用类运行时错误
            (如把 history_bars 的 ndarray 返回值当 DataFrame 用),必须靠运行时
            反馈闭环修正;与静态校验重试共享同一重试预算(MAX_ATTEMPTS)。
        :raises StrategyGenerationError: MAX_ATTEMPTS 次后仍存在 ERROR
            (exc.last_code 为最后一次提取出的代码,供经验库记录失败案例)。
        """
        feedback = ""
        if runtime_error:
            tail = "\n".join(runtime_error.strip().splitlines()[-30:])[-2500:]
            feedback = (
                "## 上一次生成的策略在回测执行阶段崩溃(运行时错误)\n"
                "以下错误发生在真实撮合执行时,静态校验未覆盖。请修复代码使回测能完整运行:\n"
                "```\n"
                f"{tail}\n"
                "```\n"
                "常见原因:history_bars 返回 numpy 结构化数组,取列为 bars['close'] 得到 ndarray,"
                "直接参与运算即可,不要再套 .values/.iloc 等 DataFrame 方法;"
                "下单前判断数组长度防越界;检查标的当日是否停牌。\n"
            )
        last_verdict: Verdict | None = None
        last_code: str | None = None
        cases_section = self._search_cases(user_text)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.attempts = attempt
            user = f"用户需求:\n{user_text}"
            if cases_section:
                user = f"{user}\n\n{cases_section}"
            if feedback:
                user = f"{user}\n\n{feedback}"
            raw = self.llm.complete(self.system, user)

            code = extract_python_block(raw)
            if code is None:
                # 输出格式违规视同 ERROR,进入下一轮重试
                last_verdict = Verdict(
                    (
                        Finding(
                            RuleId.AST_SYNTAX,
                            Severity.ERROR,
                            f"第 {attempt} 次输出未包含 ```python 代码块,无法提取策略源码。",
                        ),
                    )
                )
            else:
                last_code = code
                last_verdict = self.validator(code)

            if not last_verdict.has_error:
                return code
            feedback = _format_feedback(last_verdict, attempt)
        assert last_verdict is not None
        raise StrategyGenerationError(last_verdict, self.attempts, last_code)
