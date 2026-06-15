"""Tests for LaTeX assembly and title escaping."""

from inkwell.agent.tools.latex import assemble_latex_document, escape_latex


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
