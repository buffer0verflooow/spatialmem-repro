#!/usr/bin/env python3
"""将 Markdown 文档转换为 PDF（使用 fpdf2，支持中文）。"""

import re
import sys
import urllib.request
from pathlib import Path

from fpdf import FPDF

# Noto Sans SC 字体下载地址（Google 开源中文字体）
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
FONT_DIR = Path(__file__).resolve().parents[1] / ".fonts"
FONT_PATH = FONT_DIR / "NotoSansSC.ttf"


def ensure_font() -> Path:
    """确保中文字体可用，不存在则下载。"""
    FONT_DIR.mkdir(exist_ok=True)
    if FONT_PATH.exists() and FONT_PATH.stat().st_size > 100_000:
        return FONT_PATH

    # 回退：使用系统字体
    system_fonts = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for f in system_fonts:
        p = Path(f)
        if p.exists():
            print(f"使用系统字体: {f}")
            return p

    # 下载 Noto Sans SC
    print(f"下载中文字体 (约 16MB)...")
    urllib.request.urlretrieve(FONT_URL, str(FONT_PATH))
    print(f"字体已保存: {FONT_PATH}")
    return FONT_PATH


class MarkdownPDF(FPDF):
    """Markdown → PDF 转换器。"""

    def __init__(self, font_path: Path):
        super().__init__()
        self._font_path = str(font_path)
        self._setup_fonts()
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(20, 20, 20)

    def _setup_fonts(self):
        self.add_font("zh", "", self._font_path)
        self.add_font("zh", "B", self._font_path)
        self.add_font("zh", "I", self._font_path)
        self.add_font("mono", "", self._font_path)

    def header(self):
        if self.page_no() > 1:
            self.set_font("zh", "", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 8, "linksee-server 数据安全设计与管理方案", align="C")
            self.ln(4)
            self.set_draw_color(200, 200, 200)
            self.line(20, self.get_y(), 190, self.get_y())
            self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("zh", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"第 {self.page_no()} 页", align="C")

    def render_markdown(self, md_text: str):
        """渲染 Markdown 文本。"""
        lines = md_text.split("\n")
        i = 0
        in_code_block = False
        code_lines = []
        in_table = False
        table_rows = []

        while i < len(lines):
            line = lines[i]

            # 代码块
            if line.strip().startswith("```"):
                if in_code_block:
                    self._render_code_block(code_lines)
                    code_lines = []
                    in_code_block = False
                else:
                    # 先输出之前的表格
                    if in_table:
                        self._render_table(table_rows)
                        table_rows = []
                        in_table = False
                    in_code_block = True
                i += 1
                continue

            if in_code_block:
                code_lines.append(line)
                i += 1
                continue

            # 表格行
            if "|" in line and line.strip().startswith("|"):
                stripped = line.strip()
                if not in_table:
                    in_table = True
                    table_rows = []
                # 跳过分隔行（如 |---|---|）
                if not re.match(r"^\|[\s\-:|]+\|$", stripped):
                    cells = [c.strip() for c in stripped.split("|")[1:-1]]
                    if cells:
                        table_rows.append(cells)
                i += 1
                continue
            elif in_table:
                self._render_table(table_rows)
                table_rows = []
                in_table = False

            # 空行
            if not line.strip():
                self.ln(3)
                i += 1
                continue

            # 标题
            m = re.match(r"^(#{1,4})\s+(.*)", line)
            if m:
                level = len(m.group(1))
                text = m.group(2)
                self._render_heading(level, text)
                i += 1
                continue

            # 水平线
            if re.match(r"^-{3,}$", line.strip()) or re.match(r"^\*{3,}$", line.strip()):
                self.set_draw_color(200, 200, 200)
                self.line(20, self.get_y(), 190, self.get_y())
                self.ln(6)
                i += 1
                continue

            # 复选框列表
            m_cb = re.match(r"^(\s*)- \[([ xX])\] (.*)", line)
            if m_cb:
                checked = m_cb.group(2).lower() == "x"
                text = m_cb.group(3)
                self._render_checkbox(checked, text)
                i += 1
                continue

            # 无序列表
            m_ul = re.match(r"^(\s*)[-*]\s+(.*)", line)
            if m_ul:
                indent = len(m_ul.group(1)) // 2
                text = m_ul.group(2)
                self._render_list_item(text, indent=indent, ordered=False)
                i += 1
                continue

            # 有序列表
            m_ol = re.match(r"^(\s*)\d+\.\s+(.*)", line)
            if m_ol:
                indent = len(m_ol.group(1)) // 2
                text = m_ol.group(2)
                self._render_list_item(text, indent=indent, ordered=True)
                i += 1
                continue

            # 块引用
            if line.strip().startswith(">"):
                text = re.sub(r"^>\s*", "", line.strip())
                self._render_blockquote(text)
                i += 1
                continue

            # 普通段落
            self._render_paragraph(line.strip())
            i += 1

        # 收尾
        if in_table:
            self._render_table(table_rows)
        if in_code_block:
            self._render_code_block(code_lines)

    def _render_heading(self, level: int, text: str):
        sizes = {1: 20, 2: 15, 3: 12, 4: 11}
        colors = {1: (26, 54, 93), 2: (43, 108, 176), 3: (44, 82, 130), 4: (45, 55, 72)}

        self.ln(6 if level <= 2 else 4)
        size = sizes.get(level, 10)
        r, g, b = colors.get(level, (0, 0, 0))

        self.set_font("zh", "B", size)
        self.set_text_color(r, g, b)

        # 清理 markdown 格式
        text = self._clean_text(text)
        self.multi_cell(0, size * 0.5, text)

        # H1/H2 下划线
        if level <= 2:
            y = self.get_y()
            if level == 1:
                self.set_draw_color(43, 108, 176)
                self.set_line_width(0.8)
            else:
                self.set_draw_color(190, 227, 248)
                self.set_line_width(0.4)
            self.line(20, y, 190, y)
            self.set_line_width(0.2)

        self.ln(3)

    def _render_paragraph(self, text: str):
        self.set_font("zh", "", 10)
        self.set_text_color(30, 30, 30)
        text = self._clean_text(text)
        self.multi_cell(0, 6, text)
        self.ln(2)

    def _render_list_item(self, text: str, indent: int = 0, ordered: bool = False):
        x_base = 24 + indent * 8
        self.set_x(x_base)
        self.set_font("zh", "", 10)
        self.set_text_color(30, 30, 30)

        bullet = "  " if ordered else "• "
        text = self._clean_text(text)
        self.set_x(x_base)
        self.cell(6, 6, bullet)
        self.multi_cell(160 - indent * 8, 6, text)
        self.ln(1)

    def _render_checkbox(self, checked: bool, text: str):
        x_base = 24
        self.set_x(x_base)
        self.set_font("zh", "", 10)
        self.set_text_color(30, 30, 30)

        mark = "[x] " if checked else "[ ] "
        text = self._clean_text(text)
        self.cell(10, 6, mark)
        self.multi_cell(150, 6, text)
        self.ln(1)

    def _render_blockquote(self, text: str):
        self.set_fill_color(235, 248, 255)
        self.set_draw_color(66, 153, 225)
        self.set_line_width(0.8)

        x = self.get_x()
        y = self.get_y()
        self.set_x(x + 6)
        self.set_font("zh", "I", 10)
        self.set_text_color(42, 67, 101)

        text = self._clean_text(text)
        self.multi_cell(155, 6, text)
        end_y = self.get_y()

        # 左边蓝色竖条
        self.line(x + 4, y, x + 4, end_y)
        self.set_line_width(0.2)
        self.ln(3)

    def _render_code_block(self, lines: list[str]):
        self.set_fill_color(26, 32, 44)
        self.set_text_color(226, 232, 240)
        self.set_font("mono", "", 8)

        # 计算块高度
        line_h = 4.5
        block_h = len(lines) * line_h + 8

        # 检查是否需要换页
        if self.get_y() + block_h > 270:
            self.add_page()

        x = self.get_x()
        y = self.get_y()

        # 背景
        self.rect(x, y, 170, block_h, "F")

        self.set_xy(x + 4, y + 4)
        for line in lines:
            # 截断过长的行
            if len(line) > 90:
                line = line[:87] + "..."
            self.cell(0, line_h, line)
            self.ln(line_h)
            self.set_x(x + 4)

        self.set_y(y + block_h + 3)
        self.set_text_color(30, 30, 30)
        self.ln(2)

    def _render_table(self, rows: list[list[str]]):
        if not rows:
            return

        n_cols = len(rows[0])
        col_width = 170 / n_cols

        # 检查空间
        row_h = 7
        table_h = len(rows) * row_h + 10
        if self.get_y() + table_h > 270:
            self.add_page()

        for row_idx, row in enumerate(rows):
            # 表头
            if row_idx == 0:
                self.set_fill_color(43, 108, 176)
                self.set_text_color(255, 255, 255)
                self.set_font("zh", "B", 9)
            else:
                fill = row_idx % 2 == 0
                self.set_fill_color(247, 250, 252) if fill else self.set_fill_color(255, 255, 255)
                self.set_text_color(30, 30, 30)
                self.set_font("zh", "", 9)

            # 计算本行最大高度
            max_lines = 1
            for cell_text in row:
                cell_text = self._clean_text(cell_text)
                n_lines = max(1, len(cell_text) * self._char_width_ratio(cell_text) / (col_width - 4) + 0.5)
                max_lines = max(max_lines, int(n_lines))
            cell_h = max(row_h, max_lines * 5 + 2)

            # 检查换页
            if self.get_y() + cell_h > 270:
                self.add_page()
                # 重绘表头
                if row_idx > 0:
                    self.set_fill_color(43, 108, 176)
                    self.set_text_color(255, 255, 255)
                    self.set_font("zh", "B", 9)
                    for cell_text in rows[0]:
                        self.cell(col_width, row_h, self._clean_text(cell_text)[:30], border=1, fill=True)
                    self.ln(row_h)
                    self.set_fill_color(247, 250, 252) if fill else self.set_fill_color(255, 255, 255)
                    self.set_text_color(30, 30, 30)
                    self.set_font("zh", "", 9)

            for cell_text in row:
                cell_text = self._clean_text(cell_text)
                # 截断过长的内容
                max_chars = int(col_width / 2.2)
                if len(cell_text) > max_chars:
                    cell_text = cell_text[: max_chars - 2] + ".."
                self.cell(col_width, cell_h, cell_text, border=1, fill=True)
            self.ln(cell_h)

        self.ln(4)

    def _char_width_ratio(self, text: str) -> float:
        """估算文本宽度比例（中文字符更宽）。"""
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        return 1.0 + cjk / max(len(text), 1) * 0.8

    def _clean_text(self, text: str) -> str:
        """清理 Markdown 内联格式。"""
        # 去掉链接 [text](url)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        # 去掉粗体/斜体
        text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
        text = re.sub(r"\*([^*]*)\*", r"\1", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        # 去掉 HTML 标签
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()


def convert_md_to_pdf(md_path: str, pdf_path: str | None = None) -> str:
    md_file = Path(md_path)
    if not md_file.exists():
        print(f"错误：文件不存在 {md_path}")
        sys.exit(1)

    if pdf_path is None:
        pdf_path = str(md_file.with_suffix(".pdf"))

    print(f"正在加载字体...")
    font_path = ensure_font()

    print(f"正在转换: {md_path} → {pdf_path}")
    md_text = md_file.read_text(encoding="utf-8")

    pdf = MarkdownPDF(font_path)
    pdf.add_page()
    pdf.render_markdown(md_text)
    pdf.output(pdf_path)

    file_size = Path(pdf_path).stat().st_size / 1024
    print(f"PDF 已生成: {pdf_path} ({file_size:.1f} KB)")
    return pdf_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: .venv/bin/python scripts/md2pdf.py <markdown文件> [输出pdf路径]")
        sys.exit(1)

    md_file = sys.argv[1]
    pdf_file = sys.argv[2] if len(sys.argv) > 2 else None
    convert_md_to_pdf(md_file, pdf_file)
