"""Tests for LaTeX assembly, title escaping, and the compile_latex tool."""

import json
from pathlib import Path
from typing import cast

import pytest

from inkwell.agent.tools import latex as latex_mod
from inkwell.agent.tools.latex import (
    CompileLatexInput,
    LatexArtifacts,
    assemble_latex_document,
    escape_latex,
    make_latex_tools,
)
from lup.mcp import LupMcpTool, response_text
from lup.sandbox.container import Sandbox


class TestEscapeLatex:
    def test_escapes_special_characters(self) -> None:
        assert escape_latex("a & b") == "a \\& b"
        assert escape_latex("50% off") == "50\\% off"
        assert escape_latex("x_1") == "x\\_1"

    def test_backslash_replacement_braces_are_not_re_escaped(self) -> None:
        assert escape_latex("\\") == "\\textbackslash{}"

    def test_input_braces_are_escaped(self) -> None:
        assert escape_latex("{x}") == "\\{x\\}"


class TestAssembleLatexDocument:
    def test_wraps_body_in_a_compilable_document(self) -> None:
        result = assemble_latex_document("\\section{Intro}\nText.", "My Paper")
        assert "\\documentclass" in result
        assert "\\begin{document}" in result
        assert "\\section{Intro}" in result
        assert result.rstrip().endswith("\\end{document}")

    def test_title_is_escaped(self) -> None:
        result = assemble_latex_document("body", "Cost & Benefit")
        assert "\\title{Cost \\& Benefit}" in result

    def test_empty_title_omits_maketitle(self) -> None:
        assert "\\maketitle" not in assemble_latex_document("body", "")

    def test_body_with_preamble_passes_through(self) -> None:
        body = "\\documentclass{article}\\begin{document}x\\end{document}"
        assert assemble_latex_document(body, "T") == body


def compile_latex_tool() -> LupMcpTool:
    tools = make_latex_tools(cast(Sandbox, object()))
    return next(t for t in tools if t.name == "compile_latex")


class TestCompileLatexTool:
    async def test_missing_file_is_actionable_error(self) -> None:
        tool = compile_latex_tool()
        result = await tool.handler(
            CompileLatexInput(tex_path="/no/such/paper.tex").model_dump()
        )
        assert result.get("is_error") is True

    async def test_reports_compiled_when_pdf_produced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tex = tmp_path / "paper.tex"
        tex.write_text("\\documentclass{article}\\begin{document}x\\end{document}")

        async def fake_compile(_source: str, _sandbox: Sandbox) -> LatexArtifacts:
            return LatexArtifacts(
                tex_path=str(tex), pdf_path=str(tmp_path / "paper.pdf"), log="ok"
            )

        monkeypatch.setattr(latex_mod, "compile_tex", fake_compile)
        result = await compile_latex_tool().handler(
            CompileLatexInput(tex_path=str(tex)).model_dump()
        )
        payload = json.loads(response_text(result))
        assert payload["compiled"] is True

    async def test_reports_failure_with_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tex = tmp_path / "paper.tex"
        tex.write_text("\\begin{remark}x\\end{remark}")

        async def fake_compile(_source: str, _sandbox: Sandbox) -> LatexArtifacts:
            return LatexArtifacts(
                tex_path=str(tex),
                log="! LaTeX Error: Environment remark undefined.",
            )

        monkeypatch.setattr(latex_mod, "compile_tex", fake_compile)
        result = await compile_latex_tool().handler(
            CompileLatexInput(tex_path=str(tex)).model_dump()
        )
        payload = json.loads(response_text(result))
        assert payload["compiled"] is False
        assert "remark undefined" in payload["log"]
