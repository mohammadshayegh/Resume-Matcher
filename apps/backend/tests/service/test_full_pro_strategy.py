"""Behavioral tests for the ``full_pro`` (Full tailor Pro) tailoring intensity.

Pro is the only strategy that is allowed to broaden the resume: it appends new
job-aligned bullets and adds missing JD skills. These tests lock the two things
that make it different from ``full`` (the coverage directive reaching the LLM,
and an exhaustive skill plan) plus the guardrails that must NOT loosen with it.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.prompts.templates import (
    CRITICAL_TRUTHFULNESS_RULES,
    DIFF_COVERAGE_INSTRUCTIONS,
    DIFF_STRATEGY_INSTRUCTIONS,
    IMPROVE_PROMPT_OPTIONS,
    IMPROVE_RESUME_PROMPTS,
    SKILL_PLAN_COVERAGE_INSTRUCTIONS,
)
from app.services.improver import (
    PRO_DIFF_MAX_TOKENS,
    PRO_PROMPT_ID,
    generate_resume_diffs,
    generate_skill_target_plan,
)


def _prompt_of(mock_llm: AsyncMock) -> str:
    return mock_llm.call_args.kwargs.get("prompt") or mock_llm.call_args.args[0]


class TestProOptionIsRegistered:
    def test_option_is_offered(self) -> None:
        assert PRO_PROMPT_ID in {option["id"] for option in IMPROVE_PROMPT_OPTIONS}

    @pytest.mark.parametrize(
        "registry",
        [
            IMPROVE_RESUME_PROMPTS,
            CRITICAL_TRUTHFULNESS_RULES,
            DIFF_STRATEGY_INSTRUCTIONS,
            DIFF_COVERAGE_INSTRUCTIONS,
            SKILL_PLAN_COVERAGE_INSTRUCTIONS,
        ],
    )
    def test_every_strategy_registry_covers_each_offered_option(
        self,
        registry: dict[str, str],
    ) -> None:
        """A registry missing an id silently downgrades the user's choice."""
        assert {option["id"] for option in IMPROVE_PROMPT_OPTIONS} <= set(registry)


class TestProDiffPromptCoverage:
    @patch("app.services.improver.complete_json", new_callable=AsyncMock)
    async def test_pro_authorizes_appends_and_skill_additions(
        self,
        mock_llm: AsyncMock,
        sample_resume: dict[str, Any],
        sample_job_keywords: dict[str, Any],
    ) -> None:
        mock_llm.return_value = {"changes": [], "strategy_notes": "pro"}
        await generate_resume_diffs(
            original_resume="# Resume",
            job_description="JD",
            job_keywords=sample_job_keywords,
            prompt_id=PRO_PROMPT_ID,
            original_resume_data=sample_resume,
        )
        prompt = _prompt_of(mock_llm)
        assert "PRO MODE" in prompt
        assert "append" in prompt
        assert "add_skill" in prompt

    @patch("app.services.improver.complete_json", new_callable=AsyncMock)
    async def test_pro_keeps_fabrication_guardrails(
        self,
        mock_llm: AsyncMock,
        sample_resume: dict[str, Any],
        sample_job_keywords: dict[str, Any],
    ) -> None:
        """Broader coverage must never license invented metrics or employers."""
        mock_llm.return_value = {"changes": [], "strategy_notes": "pro"}
        await generate_resume_diffs(
            original_resume="# Resume",
            job_description="JD",
            job_keywords=sample_job_keywords,
            prompt_id=PRO_PROMPT_ID,
            original_resume_data=sample_resume,
        )
        prompt = _prompt_of(mock_llm)
        assert "never change names, companies, dates" in prompt
        assert "Do not invent metrics or achievements" in prompt
        assert "Do not add new work entries, education entries, or project entries" in prompt

    @patch("app.services.improver.complete_json", new_callable=AsyncMock)
    async def test_non_pro_strategies_stay_minimal(
        self,
        mock_llm: AsyncMock,
        sample_resume: dict[str, Any],
        sample_job_keywords: dict[str, Any],
    ) -> None:
        mock_llm.return_value = {"changes": [], "strategy_notes": "kw"}
        await generate_resume_diffs(
            original_resume="# Resume",
            job_description="JD",
            job_keywords=sample_job_keywords,
            prompt_id="keywords",
            original_resume_data=sample_resume,
        )
        prompt = _prompt_of(mock_llm)
        assert "PRO MODE" not in prompt
        assert "Apply rules 9 and 11 exactly as written" in prompt

    @patch("app.services.improver.complete_json", new_callable=AsyncMock)
    async def test_pro_gets_a_larger_output_budget(
        self,
        mock_llm: AsyncMock,
        sample_resume: dict[str, Any],
        sample_job_keywords: dict[str, Any],
    ) -> None:
        """A truncated JSON array loses the entire diff pass, so Pro needs room."""
        mock_llm.return_value = {"changes": [], "strategy_notes": "pro"}
        await generate_resume_diffs(
            original_resume="# Resume",
            job_description="JD",
            job_keywords=sample_job_keywords,
            prompt_id=PRO_PROMPT_ID,
            original_resume_data=sample_resume,
        )
        pro_tokens = mock_llm.call_args.kwargs["max_tokens"]

        await generate_resume_diffs(
            original_resume="# Resume",
            job_description="JD",
            job_keywords=sample_job_keywords,
            prompt_id="full",
            original_resume_data=sample_resume,
        )
        assert pro_tokens == PRO_DIFF_MAX_TOKENS
        assert pro_tokens > mock_llm.call_args.kwargs["max_tokens"]


class TestProSkillPlanCoverage:
    @patch("app.services.improver.complete_json", new_callable=AsyncMock)
    async def test_pro_plan_asks_for_every_jd_skill(
        self,
        mock_llm: AsyncMock,
        sample_resume: dict[str, Any],
        sample_job_keywords: dict[str, Any],
    ) -> None:
        mock_llm.return_value = {"target_skills": [], "strategy_notes": ""}
        await generate_skill_target_plan(
            original_resume_data=sample_resume,
            job_description="JD",
            job_keywords=sample_job_keywords,
            prompt_id=PRO_PROMPT_ID,
        )
        assert "EVERY required and preferred JD skill" in _prompt_of(mock_llm)

    @patch("app.services.improver.complete_json", new_callable=AsyncMock)
    async def test_default_plan_stays_conservative(
        self,
        mock_llm: AsyncMock,
        sample_resume: dict[str, Any],
        sample_job_keywords: dict[str, Any],
    ) -> None:
        mock_llm.return_value = {"target_skills": [], "strategy_notes": ""}
        await generate_skill_target_plan(
            original_resume_data=sample_resume,
            job_description="JD",
            job_keywords=sample_job_keywords,
        )
        prompt = _prompt_of(mock_llm)
        assert "EVERY required and preferred JD skill" not in prompt
        assert "conservative" in prompt
