#!/usr/bin/env python3
"""Generate the bilingual Ch-MBE GUI and RHEED labeling user manual.

The generator is intentionally self-contained and deterministic. It creates
only synthetic illustrations; no session archive, RHEED frame, prediction
file, annotation, or model checkpoint is read.

Run with the Codex bundled document Python, or any Python with ReportLab and
Pillow installed:

    python tools/rheed_postprocessing_labeling/manual/generate_manual.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    CondPageBreak,
    HRFlowable,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MANUAL_ROOT = Path(__file__).resolve().parent
ASSET_ROOT = MANUAL_ROOT / "assets"
OUTPUT_PDF = REPOSITORY_ROOT / "docs" / "RHEED_GUI_Postprocessing_Labeling_User_Manual.pdf"

FONT_REGULAR = Path(r"C:\Windows\Fonts\Deng.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\Dengb.ttf")
FONT_MONO = Path(r"C:\Windows\Fonts\consola.ttf")

NAVY = colors.HexColor("#102A43")
NAVY_2 = colors.HexColor("#183B56")
BLUE = colors.HexColor("#2563EB")
TEAL = colors.HexColor("#0F8B8D")
CYAN = colors.HexColor("#38BDF8")
ORANGE = colors.HexColor("#D97706")
RED = colors.HexColor("#B42318")
GREEN = colors.HexColor("#1B7F5A")
INK = colors.HexColor("#17212B")
MUTED = colors.HexColor("#52606D")
LIGHT = colors.HexColor("#F3F7FA")
LINE = colors.HexColor("#CBD5E1")
WHITE = colors.white

PAGE_WIDTH, PAGE_HEIGHT = A4
CONTENT_WIDTH = PAGE_WIDTH - 32 * mm


def register_fonts() -> None:
    for font in (FONT_REGULAR, FONT_BOLD, FONT_MONO):
        if not font.is_file():
            raise FileNotFoundError(f"Required local font is missing: {font}")
    pdfmetrics.registerFont(TTFont("ManualCJK", str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont("ManualCJK-Bold", str(FONT_BOLD)))
    pdfmetrics.registerFont(TTFont("ManualMono", str(FONT_MONO)))


def pil_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size=size)


def fit_pil_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start_size: int,
    *,
    bold: bool = False,
    minimum: int = 14,
) -> ImageFont.FreeTypeFont:
    size = start_size
    while size > minimum:
        font = pil_font(size, bold=bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 1
    return pil_font(minimum, bold=bold)


def rounded_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    fill: str,
    outline: str = "#CBD5E1",
    width: int = 3,
    radius: int = 18,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    lines: list[tuple[str, ImageFont.FreeTypeFont, str]],
    gap: int = 8,
) -> None:
    heights = [draw.textbbox((0, 0), text, font=font)[3] for text, font, _ in lines]
    total = sum(heights) + gap * (len(lines) - 1)
    y = box[1] + (box[3] - box[1] - total) / 2
    for (text, font, fill), height in zip(lines, heights, strict=True):
        bbox = draw.textbbox((0, 0), text, font=font)
        x = box[0] + (box[2] - box[0] - (bbox[2] - bbox[0])) / 2
        draw.text((x, y), text, font=font, fill=fill)
        y += height + gap


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], fill: str) -> None:
    draw.line((start, end), fill=fill, width=8)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = 24
    spread = 0.55
    points = [
        end,
        (
            end[0] - length * math.cos(angle - spread),
            end[1] - length * math.sin(angle - spread),
        ),
        (
            end[0] - length * math.cos(angle + spread),
            end[1] - length * math.sin(angle + spread),
        ),
    ]
    draw.polygon(points, fill=fill)


def generate_hero() -> Path:
    path = ASSET_ROOT / "synthetic_rheed_hero.png"
    image = PILImage.new("RGB", (1600, 560), "#081A2C")
    draw = ImageDraw.Draw(image)
    for y in range(image.height):
        shade = int(12 + 18 * y / image.height)
        draw.line((0, y, image.width, y), fill=(8, 26 + shade // 2, 44 + shade))
    center_y = 280
    for x, intensity in [(280, 110), (560, 170), (800, 245), (1040, 170), (1320, 110)]:
        for radius in range(82, 2, -5):
            alpha = max(10, int(intensity * (1 - radius / 88)))
            color = (min(255, 25 + alpha), min(255, 105 + alpha // 2), min(255, 130 + alpha))
            draw.ellipse((x - radius, center_y - radius // 5, x + radius, center_y + radius // 5), fill=color)
        draw.line((x, 70, x, 490), fill="#2CA9BC", width=3)
    draw.line((90, center_y, 1510, center_y), fill="#67E8F9", width=4)
    draw.text((70, 40), "SYNTHETIC RHEED ILLUSTRATION / RHEED 示意图", font=pil_font(26, bold=True), fill="#DFF8FF")
    image.save(path, format="PNG", optimize=False)
    return path


def generate_workflow() -> Path:
    path = ASSET_ROOT / "offline_workflow.png"
    image = PILImage.new("RGB", (1600, 520), "#F7FAFC")
    draw = ImageDraw.Draw(image)
    boxes = [
        (40, 140, 300, 380),
        (365, 140, 625, 380),
        (690, 140, 950, 380),
        (1015, 140, 1275, 380),
        (1340, 140, 1580, 380),
    ]
    fills = ["#E5F0FF", "#E6F7F5", "#FFF3D9", "#EDE9FE", "#E6F7ED"]
    labels = [
        ("Ch-MBE GUI", "实时采集 / Live acquisition"),
        ("Session ZIP", "原始帧与日志 / Frames + logs"),
        ("Predictions + spec", "预测与模型说明 / Model context"),
        ("Offline labeler", "后处理与分段 / Review + segments"),
        ("JSON + validation", "导出与核验 / Export + verify"),
    ]
    for index, (box, fill, label) in enumerate(zip(boxes, fills, labels, strict=True)):
        rounded_box(draw, box, fill=fill, outline="#8EA8BE", width=4, radius=24)
        main_font = fit_pil_text(draw, label[0], box[2] - box[0] - 28, 32, bold=True)
        sub_font = fit_pil_text(draw, label[1], box[2] - box[0] - 28, 23)
        centered_text(draw, box, [(label[0], main_font, "#102A43"), (label[1], sub_font, "#455B6E")], gap=18)
        if index < len(boxes) - 1:
            arrow(draw, (box[2] + 12, 260), (boxes[index + 1][0] - 12, 260), "#2563EB")
    draw.text((42, 42), "数据流 / Data flow", font=pil_font(34, bold=True), fill="#102A43")
    draw.text((42, 455), "Offline labeler never controls instruments. / 离线标注器不控制仪器。", font=pil_font(25), fill="#B42318")
    image.save(path, format="PNG", optimize=False)
    return path


def generate_launcher_mock() -> Path:
    path = ASSET_ROOT / "synthetic_labeler_window.png"
    image = PILImage.new("RGB", (1500, 900), "#E8EEF4")
    draw = ImageDraw.Draw(image)
    rounded_box(draw, (35, 35, 1465, 865), fill="#FFFFFF", outline="#56738D", width=5, radius=22)
    draw.rectangle((35, 35, 1465, 112), fill="#102A43")
    draw.text((70, 55), "RHEED Post-processing and Temporal Labeling", font=pil_font(34, bold=True), fill="white")
    draw.text((70, 130), "离线构建时间轴并标记重构分段 / Build an offline timeline and label temporal segments", font=pil_font(25), fill="#314A60")
    rounded_box(draw, (70, 170, 1430, 235), fill="#FFF5E8", outline="#E9A23B", width=3, radius=12)
    draw.text((92, 184), "模型输出可见：仅为 model-assisted review，不是 blind-gold。", font=pil_font(25, bold=True), fill="#8A3B12")
    draw.text((92, 211), "Model outputs are visible: exports are not eligible as blind-gold labels.", font=pil_font(19), fill="#8A3B12")

    draw.text((75, 265), "Session ZIP / 会话压缩包", font=pil_font(21, bold=True), fill="#102A43")
    rounded_box(draw, (390, 255, 1245, 302), fill="#F8FAFC", outline="#B9C7D4", width=2, radius=8)
    draw.text((410, 266), r"<local-data-root>\growth_session.zip", font=pil_font(19), fill="#34495E")
    rounded_box(draw, (1265, 255, 1428, 302), fill="#EAF1F8", outline="#8EA8BE", width=2, radius=8)
    draw.text((1308, 266), "Browse", font=pil_font(19, bold=True), fill="#102A43")

    draw.text((75, 325), "Ordered model pairs", font=pil_font(19, bold=True), fill="#102A43")
    draw.text((75, 352), "有序模型配对", font=pil_font(19, bold=True), fill="#102A43")
    table_box = (390, 320, 1428, 485)
    draw.rectangle(table_box, fill="#FFFFFF", outline="#8EA8BE", width=3)
    draw.rectangle((390, 320, 1428, 362), fill="#DDE9F3", outline="#8EA8BE", width=2)
    draw.line((900, 320, 900, 485), fill="#8EA8BE", width=2)
    draw.text((410, 330), "Prediction CSV", font=pil_font(19, bold=True), fill="#102A43")
    draw.text((920, 330), "Model-spec JSON", font=pil_font(19, bold=True), fill="#102A43")
    draw.text((410, 380), r"<local-data-root>\predictions.csv", font=pil_font(18), fill="#34495E")
    draw.text((920, 380), r"<local-data-root>\model_spec.json", font=pil_font(18), fill="#34495E")
    draw.line((390, 425, 1428, 425), fill="#D4DEE7", width=2)
    buttons = [
        (390, 500, 585, 540, "Add pair"),
        (600, 500, 845, 540, "Remove selected"),
        (860, 500, 1025, 540, "Move up"),
        (1040, 500, 1225, 540, "Move down"),
    ]
    for x0, y0, x1, y1, label in buttons:
        rounded_box(draw, (x0, y0, x1, y1), fill="#EAF1F8", outline="#8EA8BE", width=2, radius=7)
        font = fit_pil_text(draw, label, x1 - x0 - 16, 18, bold=True)
        centered_text(draw, (x0, y0, x1, y1), [(label, font, "#102A43")], gap=0)

    draw.text((75, 580), "Output directory / 输出目录", font=pil_font(21, bold=True), fill="#102A43")
    rounded_box(draw, (390, 570, 1245, 617), fill="#F8FAFC", outline="#B9C7D4", width=2, radius=8)
    draw.text((410, 581), r"<local-data-root>\growth_session_labeling_report", font=pil_font(18), fill="#34495E")
    rounded_box(draw, (1265, 570, 1428, 617), fill="#EAF1F8", outline="#8EA8BE", width=2, radius=8)
    draw.text((1308, 581), "Browse", font=pil_font(19, bold=True), fill="#102A43")

    draw.text((75, 645), "Report title / 报告标题", font=pil_font(21, bold=True), fill="#102A43")
    rounded_box(draw, (390, 635, 905, 682), fill="#F8FAFC", outline="#B9C7D4", width=2, radius=8)
    draw.text((410, 646), "RHEED reconstruction timeline", font=pil_font(18), fill="#34495E")
    draw.text((935, 645), "WebP quality", font=pil_font(20, bold=True), fill="#102A43")
    rounded_box(draw, (1130, 635, 1245, 682), fill="#F8FAFC", outline="#B9C7D4", width=2, radius=8)
    draw.text((1170, 646), "78", font=pil_font(19), fill="#34495E")

    rounded_box(draw, (390, 715, 825, 778), fill="#2563EB", outline="#1E4EA8", width=3, radius=12)
    draw.text((451, 730), "Build report and open", font=pil_font(25, bold=True), fill="white")
    rounded_box(draw, (855, 715, 1428, 778), fill="#F2F7FB", outline="#B9C7D4", width=2, radius=10)
    draw.text((875, 727), "Status / 状态: Ready", font=pil_font(20, bold=True), fill="#1B7F5A")
    draw.text((875, 752), "Messages appear here / 消息显示在此", font=pil_font(17), fill="#52606D")
    draw.text((1165, 832), "SYNTHETIC UI / 界面示意", font=pil_font(18, bold=True), fill="#64748B")
    image.save(path, format="PNG", optimize=False)
    return path


def generate_timeline_mock() -> Path:
    path = ASSET_ROOT / "synthetic_timeline_editor.png"
    image = PILImage.new("RGB", (1600, 950), "#F8FAFC")
    draw = ImageDraw.Draw(image)
    draw.text((55, 38), "RHEED reconstruction timeline / RHEED 重构时间轴", font=pil_font(36, bold=True), fill="#102A43")
    draw.text((55, 92), "Model-assisted review - not blind-gold / 模型辅助审阅 - 非盲标金标准", font=pil_font(24, bold=True), fill="#B42318")

    rounded_box(draw, (55, 145, 1020, 470), fill="#FFFFFF", outline="#B9C7D4", width=3, radius=14)
    draw.text((80, 170), "Human reconstruction segments / 人工重构分段", font=pil_font(27, bold=True), fill="#102A43")
    draw.line((110, 320, 960, 320), fill="#8EA8BE", width=4)
    segments = [
        (170, 350, "#93C5FD", "1x1 / none-weak"),
        (350, 610, "#F6C98B", "Twinned (2x1)"),
        (610, 820, "#A7F3D0", "c(6x2)"),
        (820, 925, "#C4B5FD", "RT13"),
    ]
    for x0, x1, fill, label in segments:
        draw.rectangle((x0, 265, x1, 355), fill=fill, outline="#52606D", width=2)
        font = fit_pil_text(draw, label, x1 - x0 - 12, 20, bold=True, minimum=13)
        bbox = draw.textbbox((0, 0), label, font=font)
        draw.text((x0 + (x1 - x0 - (bbox[2] - bbox[0])) / 2, 290), label, font=font, fill="#102A43")
    draw.line((545, 220, 545, 405), fill="#2563EB", width=6)
    draw.polygon([(535, 220), (555, 220), (545, 205)], fill="#2563EB")
    draw.text((80, 405), "Mark In / 标记起点", font=pil_font(21, bold=True), fill="#102A43")
    draw.text((345, 405), "Mark Out / 标记终点", font=pil_font(21, bold=True), fill="#102A43")
    draw.text((650, 405), "Add / Update / Undo / 添加、修改、撤销", font=pil_font(21), fill="#52606D")

    rounded_box(draw, (1060, 145, 1545, 610), fill="#0A1830", outline="#385470", width=3, radius=14)
    draw.text((1090, 165), "Selected frame / 当前帧", font=pil_font(25, bold=True), fill="#E6F7FF")
    for x in (1150, 1300, 1450):
        draw.ellipse((x - 34, 320 - 11, x + 34, 320 + 11), fill="#B9FFF6")
        draw.line((x, 235, x, 480), fill="#2E8192", width=3)
    draw.line((1100, 320, 1505, 320), fill="#71E3EF", width=4)
    draw.text((1130, 548), "SYNTHETIC RHEED / 示意图", font=pil_font(20, bold=True), fill="#9CCBD5")

    rounded_box(draw, (55, 520, 1020, 895), fill="#FFFFFF", outline="#B9C7D4", width=3, radius=14)
    draw.text((80, 545), "Model probabilities / 模型概率", font=pil_font(26, bold=True), fill="#102A43")
    chart = (115, 615, 960, 830)
    draw.line((chart[0], chart[3], chart[2], chart[3]), fill="#60758A", width=3)
    draw.line((chart[0], chart[1], chart[0], chart[3]), fill="#60758A", width=3)
    lines = [
        ("#2563EB", [(115, 665), (300, 690), (500, 720), (700, 760), (960, 790)]),
        ("#0F9D76", [(115, 790), (300, 765), (500, 720), (700, 690), (960, 650)]),
        ("#D97706", [(115, 815), (300, 805), (500, 785), (700, 765), (960, 740)]),
    ]
    for color, points in lines:
        draw.line(points, fill=color, width=6, joint="curve")
    draw.line((545, 600, 545, 835), fill="#2563EB", width=5)
    draw.text((1090, 670), "Display only / 仅显示调整", font=pil_font(24, bold=True), fill="#102A43")
    draw.text((1090, 715), "Brightness  100%", font=pil_font(22), fill="#314A60")
    draw.text((1090, 760), "Contrast     100%", font=pil_font(22), fill="#314A60")
    draw.text((1090, 815), "Zoom / 放大", font=pil_font(22), fill="#314A60")
    draw.text((1090, 865), "Pixels and predictions remain unchanged.", font=pil_font(19), fill="#52606D")
    image.save(path, format="PNG", optimize=False)
    return path


def generate_assets() -> dict[str, Path]:
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    return {
        "hero": generate_hero(),
        "workflow": generate_workflow(),
        "launcher": generate_launcher_mock(),
        "timeline": generate_timeline_mock(),
    }


def make_styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "CoverTitle", parent=sample["Title"], fontName="ManualCJK-Bold",
            fontSize=25, leading=31, textColor=NAVY, alignment=TA_LEFT, spaceAfter=5 * mm,
            wordWrap="CJK",
        ),
        "cover_sub": ParagraphStyle(
            "CoverSub", parent=sample["Normal"], fontName="ManualCJK",
            fontSize=12, leading=18, textColor=MUTED, alignment=TA_LEFT, wordWrap="CJK",
        ),
        "h1": ParagraphStyle(
            "H1", parent=sample["Heading1"], fontName="ManualCJK-Bold",
            fontSize=18, leading=23, textColor=NAVY, spaceAfter=4 * mm, wordWrap="CJK",
        ),
        "h2": ParagraphStyle(
            "H2", parent=sample["Heading2"], fontName="ManualCJK-Bold",
            fontSize=12, leading=16, textColor=NAVY_2, spaceBefore=2.5 * mm,
            spaceAfter=1.5 * mm, wordWrap="CJK",
        ),
        "body": ParagraphStyle(
            "BodyBi", parent=sample["BodyText"], fontName="ManualCJK",
            fontSize=9.1, leading=13.6, textColor=INK, spaceAfter=2.2 * mm,
            wordWrap="CJK",
        ),
        "small": ParagraphStyle(
            "SmallBi", parent=sample["BodyText"], fontName="ManualCJK",
            fontSize=7.8, leading=11.2, textColor=MUTED, wordWrap="CJK",
        ),
        "tiny": ParagraphStyle(
            "TinyBi", parent=sample["BodyText"], fontName="ManualCJK",
            fontSize=6.8, leading=9.3, textColor=MUTED, wordWrap="CJK",
        ),
        "callout": ParagraphStyle(
            "Callout", parent=sample["BodyText"], fontName="ManualCJK",
            fontSize=9.2, leading=13.7, textColor=INK, wordWrap="CJK",
        ),
        "table": ParagraphStyle(
            "TableCell", parent=sample["BodyText"], fontName="ManualCJK",
            fontSize=7.5, leading=10.5, textColor=INK, wordWrap="CJK",
        ),
        "table_head": ParagraphStyle(
            "TableHead", parent=sample["BodyText"], fontName="ManualCJK-Bold",
            fontSize=7.5, leading=10, textColor=WHITE, wordWrap="CJK",
        ),
        "code": ParagraphStyle(
            "Code", parent=sample["Code"], fontName="ManualMono",
            fontSize=7.4, leading=10.5, textColor=colors.HexColor("#DCEBFA"),
            backColor=NAVY, borderPadding=8, leftIndent=0, rightIndent=0,
            spaceBefore=1.5 * mm, spaceAfter=2.5 * mm,
        ),
        "step_num": ParagraphStyle(
            "StepNumber", parent=sample["Normal"], fontName="ManualCJK-Bold",
            fontSize=14, leading=16, textColor=WHITE, alignment=TA_CENTER,
        ),
    }


class ManualCanvas(canvas.Canvas):
    """Canvas that adds stable bilingual headers and page X / Y footers."""

    PAGE_LABELS = {
        2: "Workflow / 工作流程",
        3: "Startup / 启动",
        4: "Labeler desktop UI / 标注器桌面界面",
        5: "Input contract / 输入约定",
        6: "Build report / 构建报告",
        7: "Temporal labeling / 时间分段标注",
        8: "Review controls / 审阅控制",
        9: "Export and validate / 导出与验证",
        10: "Data safety / 数据安全",
        11: "Troubleshooting / 故障排查",
        12: "Version and checklist / 版本与检查表",
    }

    def __init__(self, *args, **kwargs):
        # BaseDocTemplate passes ``invariant=None`` explicitly, so setdefault()
        # would leave volatile PDF timestamps and document IDs in place.
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict] = []
        self.setTitle("Ch-MBE GUI and RHEED Post-processing Labeling User Manual")
        self.setAuthor("AI4MBE")
        self.setSubject("Bilingual operating guide for Ch-MBE GUI and offline RHEED temporal labeling")

    def showPage(self) -> None:  # noqa: N802 - ReportLab API
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_chrome(total)
            super().showPage()
        super().save()

    def _draw_chrome(self, total: int) -> None:
        page = self._pageNumber
        self.saveState()
        if page == 1:
            self.setFillColor(NAVY)
            self.rect(0, 0, PAGE_WIDTH, 12 * mm, fill=1, stroke=0)
            self.setFillColor(WHITE)
            self.setFont("ManualCJK", 7.5)
            self.drawString(16 * mm, 5 * mm, "AI4MBE | Ch-MBE | Manual v1.0 | 2026-08-10")
            self.drawRightString(PAGE_WIDTH - 16 * mm, 5 * mm, f"Page {page} / {total}")
        else:
            self.setStrokeColor(LINE)
            self.setLineWidth(0.6)
            self.line(16 * mm, PAGE_HEIGHT - 14 * mm, PAGE_WIDTH - 16 * mm, PAGE_HEIGHT - 14 * mm)
            self.setFillColor(NAVY)
            self.setFont("ManualCJK-Bold", 7.5)
            self.drawString(16 * mm, PAGE_HEIGHT - 10.5 * mm, "AI4MBE | Ch-MBE")
            self.setFont("ManualCJK", 7.2)
            self.setFillColor(MUTED)
            self.drawRightString(PAGE_WIDTH - 16 * mm, PAGE_HEIGHT - 10.5 * mm, self.PAGE_LABELS.get(page, "User manual / 用户手册"))
            self.line(16 * mm, 13 * mm, PAGE_WIDTH - 16 * mm, 13 * mm)
            self.setFont("ManualCJK", 7)
            self.drawString(16 * mm, 8 * mm, "Model-assisted review - not blind-gold / 模型辅助审阅 - 非盲标金标准")
            self.drawRightString(PAGE_WIDTH - 16 * mm, 8 * mm, f"Page {page} / {total}")
        self.restoreState()


def p(styles: dict[str, ParagraphStyle], zh: str, en: str, style: str = "body") -> Paragraph:
    return Paragraph(
        f"<b>{zh}</b><br/><font color='#52606D'>{en}</font>",
        styles[style],
    )


def plain(styles: dict[str, ParagraphStyle], text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, styles[style])


def heading(styles: dict[str, ParagraphStyle], number: str, zh: str, en: str) -> Paragraph:
    return Paragraph(f"{number}　{zh}<br/><font size='11' color='#52606D'>{en}</font>", styles["h1"])


def subheading(styles: dict[str, ParagraphStyle], zh: str, en: str) -> Paragraph:
    return Paragraph(f"{zh} <font color='#52606D'>/ {en}</font>", styles["h2"])


def bullet(styles: dict[str, ParagraphStyle], zh: str, en: str) -> Paragraph:
    return Paragraph(
        f"<b>{zh}</b><br/><font color='#52606D'>{en}</font>",
        ParagraphStyle(
            "BulletTemp", parent=styles["body"], leftIndent=5 * mm,
            firstLineIndent=0, bulletIndent=0, spaceAfter=1.6 * mm,
        ),
        bulletText="•",
    )


def callout(
    styles: dict[str, ParagraphStyle],
    title: str,
    zh: str,
    en: str,
    *,
    tone: str = "info",
) -> Table:
    palette = {
        "info": (colors.HexColor("#EAF3FF"), BLUE),
        "safe": (colors.HexColor("#E8F6EF"), GREEN),
        "warn": (colors.HexColor("#FFF4E5"), ORANGE),
        "danger": (colors.HexColor("#FDECEC"), RED),
    }
    background, accent = palette[tone]
    content = Paragraph(
        f"<b><font color='{accent.hexval()}'>{title}</font></b><br/>{zh}<br/>"
        f"<font color='#52606D'>{en}</font>",
        styles["callout"],
    )
    table = Table([["", content]], colWidths=[3 * mm, CONTENT_WIDTH - 3 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("BACKGROUND", (0, 0), (0, -1), accent),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (1, 0), (1, 0), 4 * mm),
        ("RIGHTPADDING", (1, 0), (1, 0), 4 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ("BOX", (0, 0), (-1, -1), 0.5, accent),
    ]))
    return table


def step(styles: dict[str, ParagraphStyle], number: int, zh: str, en: str) -> Table:
    number_cell = Table([[Paragraph(str(number), styles["step_num"])]], colWidths=[10 * mm], rowHeights=[10 * mm])
    number_cell.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLUE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0, BLUE),
    ]))
    text = p(styles, zh, en, "body")
    table = Table([[number_cell, text]], colWidths=[13 * mm, CONTENT_WIDTH - 13 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
    ]))
    return table


def table(
    styles: dict[str, ParagraphStyle],
    rows: list[list[str]],
    widths: list[float],
    *,
    header: bool = True,
) -> Table:
    converted: list[list[Paragraph]] = []
    for row_index, row in enumerate(rows):
        style = styles["table_head"] if header and row_index == 0 else styles["table"]
        converted.append([Paragraph(value, style) for value in row])
    result = Table(converted, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.8 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8 * mm),
    ]
    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), NAVY_2))
        commands.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT]))
    else:
        commands.append(("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT]))
    result.setStyle(TableStyle(commands))
    return result


def manual_image(path: Path, width: float = CONTENT_WIDTH) -> Image:
    with PILImage.open(path) as source:
        height = width * source.height / source.width
    result = Image(str(path), width=width, height=height)
    result.hAlign = "CENTER"
    return result


def code(styles: dict[str, ParagraphStyle], text: str) -> Table:
    block = Preformatted(text.strip("\n"), styles["code"])
    result = Table([[block]], colWidths=[CONTENT_WIDTH], hAlign="LEFT")
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.7, NAVY_2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
    ]))
    return result


def page_break(story: list) -> None:
    story.append(PageBreak())


def build_story(styles: dict[str, ParagraphStyle], assets: dict[str, Path]) -> list:
    story: list = []

    # Page 1 - cover
    story.extend([
        Spacer(1, 5 * mm),
        Paragraph("Ch-MBE GUI 与 RHEED 后处理标注", styles["cover_title"]),
        Paragraph("Ch-MBE GUI and RHEED Post-processing Labeling", styles["cover_sub"]),
        Spacer(1, 5 * mm),
        manual_image(assets["hero"], CONTENT_WIDTH),
        Spacer(1, 6 * mm),
        callout(
            styles,
            "核心边界 / Core boundary",
            "后处理标注器只读取归档数据，不连接或控制相机、温度计、电源及其他仪器。",
            "The post-processing labeler reads archived data only. It does not connect to or control cameras, pyrometers, power supplies, or other instruments.",
            tone="safe",
        ),
        Spacer(1, 6 * mm),
        table(styles, [
            ["快速入口 / Quick entry", "双击 / Double-click"],
            ["Ch-MBE 实时 GUI / Live GUI", "Start Ch-MBE Growth Monitor.cmd"],
            ["RHEED 离线标注器 / Offline labeler", "Start RHEED Post-processing Labeler.cmd"],
            ["安装或更新桌面快捷方式 / Install or refresh shortcuts", "Install AI4MBE Desktop Shortcuts.cmd"],
        ], [58 * mm, CONTENT_WIDTH - 58 * mm]),
        Spacer(1, 5 * mm),
        p(styles, "版本：Manual v1.0，发布日期：2026-08-10。适用于 Windows 上的 Ch-MBE 工作站。", "Version: Manual v1.0, issued 2026-08-10. Intended for the Windows Ch-MBE workstation.", "small"),
    ])

    # Page 2 - overview and contents
    page_break(story)
    story.extend([
        heading(styles, "1", "先理解两条独立路径", "Understand the two independent paths"),
        p(styles, "实时 GUI 用于采集和显示；离线标注器只处理已经结束并打包的会话。不要把离线工具的结果解释为实时控制指令。", "The live GUI performs acquisition and display. The offline labeler works only on completed archived sessions. Never interpret offline output as a real-time control instruction."),
        manual_image(assets["workflow"]),
        Spacer(1, 3 * mm),
        subheading(styles, "本手册包含", "This manual covers"),
        table(styles, [
            ["页 / Page", "内容 / Topic", "结果 / Outcome"],
            ["3", "快捷方式和 Ch-MBE GUI 启动 / Shortcuts and live GUI startup", "确认启动正确且正常退出"],
            ["4", "标注器桌面界面 / Labeler desktop UI", "选择输入、构建、打开、验证"],
            ["5-6", "输入和安全构建 / Inputs and safe build", "得到完整本地报告目录"],
            ["7-8", "分段标注和显示控制 / Segments and display controls", "按真实保存帧建立可追溯标签"],
            ["9", "导出和验证 / Export and validation", "得到 canonical JSON 并 fail-closed 验证"],
            ["10-12", "数据安全、排障和版本 / Safety, troubleshooting, version", "安全交接和复现"],
        ], [20 * mm, 80 * mm, CONTENT_WIDTH - 100 * mm]),
        Spacer(1, 3 * mm),
        callout(styles, "术语 / Terminology", "RHEED 重构标签、采集质量 QC、FeSe 薄膜质量是三个不同概念。", "Reconstruction labels, acquisition-quality QC, and FeSe film quality are three different concepts.", tone="warn"),
    ])

    # Page 3 - startup
    page_break(story)
    story.extend([
        heading(styles, "2", "安装快捷方式并启动 Ch-MBE GUI", "Install shortcuts and start the Ch-MBE GUI"),
        subheading(styles, "首次设置或版本更新后", "First setup or after an update"),
        step(styles, 1, "在 GUI 仓库根目录双击 Install AI4MBE Desktop Shortcuts.cmd。", "From the GUI repository root, double-click Install AI4MBE Desktop Shortcuts.cmd."),
        step(styles, 2, "确认桌面上出现或更新两个入口：Ch-MBE Growth Monitor 和 RHEED Post-processing Labeler。", "Confirm that the desktop now has refreshed entries for Ch-MBE Growth Monitor and RHEED Post-processing Labeler."),
        step(styles, 3, "若 Windows 显示意外的安全警告，不要绕过；停止并联系维护者核对文件来源和 commit。", "If Windows shows an unexpected security warning, do not bypass it. Stop and ask the maintainer to verify the file source and commit."),
        subheading(styles, "每次实验启动", "Start each experiment"),
        step(styles, 1, "双击 Start Ch-MBE Growth Monitor.cmd 或对应桌面快捷方式。不要从 O-MBE 入口启动。", "Double-click Start Ch-MBE Growth Monitor.cmd or its desktop shortcut. Do not use the O-MBE launcher."),
        step(styles, 2, "等待主窗口出现，确认标题或 chamber 信息为 Ch-MBE，并查看各设备状态。", "Wait for the main window, confirm the title or chamber identity is Ch-MBE, and inspect each device status."),
        step(styles, 3, "只有在操作员准备好后才按实验 SOP ARM 或开始会话；双击启动本身不授权改变仪器设定值。", "ARM or begin a session only when the operator is ready under the experiment SOP. Double-click startup alone does not authorize setpoint changes."),
        step(styles, 4, "结束时使用 GUI 的正常关闭方式并等待窗口消失。不要在日志写入期间强制结束 Python。", "Close through the GUI normally and wait for the window to disappear. Do not force-kill Python while logs are being written."),
        Spacer(1, 3 * mm),
        callout(styles, "预期环境 / Expected environment", "启动器日志位于 %LOCALAPPDATA%\\AI4MBE\\LauncherLogs，并显示实际解析到的 Python 和仓库路径。核对其符合该工作站配置；不要假设固定盘符，也不要随意新增全局环境变量。", "Launcher logs are under %LOCALAPPDATA%\\AI4MBE\\LauncherLogs and show the resolved Python and repository paths. Verify them against that workstation's configuration; do not assume a fixed drive letter or add global environment variables ad hoc.", tone="info"),
        subheading(styles, "启动后快速检查", "Quick check after startup"),
        table(styles, [
            ["检查 / Check", "正常 / Expected", "异常时 / If abnormal"],
            ["Chamber", "显示 Ch-MBE 配置", "立即关闭，核对启动器"],
            ["RHEED", "新帧/sequence 继续更新", "不要保存旧帧；查窗口/相机状态"],
            ["Temperature", "读数与 valid/connected 一致", "记录模式和错误；不要猜测串口参数"],
            ["Logs", "会话开始后写入指定目录", "先保留错误文本，再联系维护者"],
        ], [34 * mm, 65 * mm, CONTENT_WIDTH - 99 * mm]),
    ])

    # Page 4 - labeler desktop UI
    page_break(story)
    story.extend([
        heading(styles, "3", "打开 RHEED 后处理标注器", "Open the RHEED post-processing labeler"),
        step(styles, 1, "双击 Start RHEED Post-processing Labeler.cmd 或其桌面快捷方式。", "Double-click Start RHEED Post-processing Labeler.cmd or its desktop shortcut."),
        step(styles, 2, "标注器应显示 Build、Open existing report、Validate 和 Open PDF manual。它不会启动 Growth Monitor。", "The labeler should show Build, Open existing report, Validate, and Open PDF manual. It does not start Growth Monitor."),
        manual_image(assets["launcher"], CONTENT_WIDTH),
        Spacer(1, 2 * mm),
        table(styles, [
            ["区域 / Area", "用途 / Purpose"],
            ["Session ZIP", "选择一个归档的 Growth Monitor 会话 / Select one archived session"],
            ["Model inputs", "每行配对一个 predictions CSV 和 model-spec JSON / One positional pair per row"],
            ["Output directory", "必须是新目录或空目录，推荐仓库外 / New or empty, preferably outside Git"],
            ["Message log", "保留构建/验证成功或失败的完整信息 / Preserve complete build or validation output"],
        ], [42 * mm, CONTENT_WIDTH - 42 * mm]),
        Spacer(1, 2 * mm),
        callout(styles, "离线 / Offline", "此窗口可以在没有仪器软件的电脑上使用，只要输入文件完整且 Python 环境正确。", "This window can run on a computer without instrument software, provided the archived inputs and Python environment are complete.", tone="safe"),
    ])

    # Page 5 - inputs
    page_break(story)
    story.extend([
        heading(styles, "4", "准备输入并核对溯源", "Prepare inputs and verify provenance"),
        subheading(styles, "会话压缩包", "Session ZIP"),
        p(styles, "压缩包必须包含一个 session_metadata.json、一个 heartbeat_log.csv，以及 heartbeat 记录引用的 frames/ 图像。不要解压后手动重命名帧。", "The archive must contain one session_metadata.json, one heartbeat_log.csv, and the frames/ images referenced by heartbeat rows. Do not manually rename extracted frames."),
        code(styles, "growth_session.zip\n  session_metadata.json\n  heartbeat_log.csv\n  frames/\n    heartbeat_000001_....bmp\n    heartbeat_000002_....bmp"),
        subheading(styles, "预测表与模型说明必须成对", "Predictions and model specs are positional pairs"),
        table(styles, [
            ["文件 / File", "必须包含 / Must contain", "检查 / Check"],
            ["Prediction CSV", "frame_index, heartbeat_idx, elapsed_s, captured_at_utc, capture_sequence, frame_name, frame_sha256", "行顺序与保存帧完全一致"],
            ["Model-spec JSON", "key, title, classes, probability_columns, status, provenance", "类别顺序与概率列一一对应"],
            ["Session ZIP", "metadata, heartbeat, frames", "每个 frame_path 在 ZIP 内存在"],
        ], [35 * mm, 92 * mm, CONTENT_WIDTH - 127 * mm]),
        Spacer(1, 3 * mm),
        p(styles, "prediction CSV 的 frame_index 可以从 0 或 1 开始，但随后必须连续。heartbeat_idx、capture_sequence 和 elapsed_s 可以有间隔；工具按真实保存帧和真实时间工作，不假设严格 1 Hz。", "Prediction frame_index may begin at 0 or 1, then must remain contiguous. heartbeat_idx, capture_sequence, and elapsed_s may have gaps. The tool follows actual saved frames and recorded times; it never assumes exact 1 Hz sampling."),
        callout(styles, "Fail closed", "任何行数、顺序、时间、sequence、文件名或 SHA-256 不匹配都会停止构建。不要编辑输入来绕过错误。", "Any mismatch in row count, order, time, sequence, filename, or SHA-256 stops the build. Do not edit inputs to bypass the error.", tone="danger"),
        subheading(styles, "推荐目录", "Recommended local layout"),
        code(styles, "<local-data-root>\\2026-08-10_anneal\\\n  source\\growth_session.zip\n  predictions\\model_A.csv\n  specs\\model_A.json\n  output\\              # choose this new/empty folder\n  exports\\             # annotation JSON/CSV"),
    ])

    # Page 6 - build
    page_break(story)
    story.extend([
        heading(styles, "5", "安全构建并打开报告", "Build and open a report safely"),
        step(styles, 1, "在 Session ZIP 中选择原始归档副本。", "Choose the preserved archive copy in Session ZIP."),
        step(styles, 2, "对每个要展示的模型添加一行 Prediction CSV + Model-spec JSON；顺序必须配对。", "Add one Prediction CSV + Model-spec JSON row for every model to display; pairing is positional."),
        step(styles, 3, "选择仓库外的新目录或空目录。桌面标注器不会覆盖非空目录。", "Choose a new or empty directory outside the repository. The desktop labeler will not overwrite a non-empty directory."),
        step(styles, 4, "填写报告标题；review quality 只影响审阅图，不改变原始图或模型输出。默认 78 适合一般使用。", "Enter a report title. Review quality changes only review images, never archived pixels or predictions. The default 78 is suitable for routine use."),
        step(styles, 5, "点击 Build report and open，等待状态为成功并自动打开 interactive_report.html。", "Click Build report and open. Wait for a success status and automatic opening of interactive_report.html."),
        subheading(styles, "PowerShell 备用方式", "PowerShell fallback"),
        code(styles, "Set-Location '<GUI repository>'\nconda activate ai4mbe-gui\npython -m tools.rheed_postprocessing_labeling build `\n  --session '<local-data-root>\\growth_session.zip' `\n  --predictions '<local-data-root>\\predictions.csv' `\n  --model-spec '<local-data-root>\\model_spec.json' `\n  --output-dir '<local-data-root>\\labeling-output'"),
        subheading(styles, "不要只复制 HTML", "Do not copy the HTML alone"),
        p(styles, "报告目录包含 interactive_report.html、images/、vendor/ 和 run_manifest.json。移动或交接时复制整个目录；同时在报告目录外单独保留原始 prediction CSV 和 model-spec JSON。", "The report directory contains interactive_report.html, images/, vendor/, and run_manifest.json. Copy the whole directory when moving or handing it off, and preserve the original prediction CSV and model-spec JSON separately outside the report directory."),
        callout(styles, "--overwrite 的边界 / --overwrite boundary", "桌面界面始终拒绝非空输出目录。高级 CLI 的 --overwrite 只允许替换已含 run_manifest.json 和 interactive_report.html 的有效工具输出，并在替换前先验证本次输入；它仍拒绝文件系统根、仓库/源码祖先、当前目录和含输入文件的目录。", "The desktop UI always refuses a non-empty output directory. Advanced CLI --overwrite may replace only a recognized tool output containing run_manifest.json and interactive_report.html, and it validates the new inputs before replacement. It still refuses filesystem roots, repository/source ancestors, the current directory, and directories containing inputs.", tone="warn"),
    ])

    # Page 7 - annotation
    page_break(story)
    story.extend([
        heading(styles, "6", "像剪辑软件一样做时间分段标注", "Label temporal segments like an editing timeline"),
        manual_image(assets["timeline"], CONTENT_WIDTH),
        Spacer(1, 2 * mm),
        step(styles, 1, "拖动 Frame playhead 或点击任意模型曲线，找到分段的第一张保存帧。点击 Mark In。", "Move the Frame playhead or click a model plot to find the first saved frame. Click Mark In."),
        step(styles, 2, "移动到该分段的最后一张保存帧，点击 Mark Out。Out 是包含端点。", "Move to the final saved frame in the segment and click Mark Out. Out is inclusive."),
        step(styles, 3, "选择重构标签，填写 Labeler，按需添加 Notes，然后点击 Add segment。", "Choose a reconstruction label, enter Labeler, add Notes if useful, then click Add segment."),
        step(styles, 4, "点击已保存分段可修改；Update 保留 annotation_id。相邻分段允许，重叠分段会被拒绝。", "Select a saved segment to edit it. Update preserves annotation_id. Adjacent segments are allowed; overlaps are rejected."),
        p(styles, "界面显示和导出的 saved-frame ordinal 从 1 开始。每个端点同时记录 heartbeat、capture sequence、UTC 和 frame SHA-256，因此即使采样有间隔也可追溯。", "Visible and exported saved-frame ordinals are 1-based. Every endpoint also records heartbeat, capture sequence, UTC, and frame SHA-256, so it remains traceable despite sampling gaps."),
    ])

    # Page 8 - review controls
    page_break(story)
    story.extend([
        heading(styles, "7", "审阅、显示调整和草稿", "Review, display controls, and drafts"),
        subheading(styles, "所有时间指示器保持同步", "All time indicators stay synchronized"),
        table(styles, [
            ["操作 / Action", "应同时变化 / Must update together", "不应变化 / Must not change"],
            ["拖动主时间轴", "当前 RHEED 图、模型 guide、放大窗口时间轴", "归档文件和预测值"],
            ["点击模型曲线", "主 playhead、图像、metadata", "已保存分段"],
            ["Brightness / Contrast", "屏幕显示", "像素、SHA-256、模型输出"],
            ["Enlarge", "放大视图和可调时间轴", "原图、标注边界"],
        ], [42 * mm, 72 * mm, CONTENT_WIDTH - 114 * mm]),
        subheading(styles, "草稿与编辑", "Draft and editing"),
        bullet(styles, "浏览器按 dataset_id 在 localStorage 中保存草稿；它不是长期备份。", "The browser stores a dataset-scoped draft in localStorage; this is not a durable backup."),
        bullet(styles, "频繁 Export JSON，特别是在长会话、换电脑、换浏览器或清理缓存前。", "Export JSON frequently, especially for long sessions and before changing computers, browsers, or cache settings."),
        bullet(styles, "Delete selected 需要确认；Undo 只恢复最近一次标注修改。", "Delete selected requires confirmation. Undo restores only the most recent annotation mutation."),
        bullet(styles, "Import JSON 会先严格验证；失败时当前分段集合保持不变。", "Import JSON validates before replacement. On failure, the current segment set remains unchanged."),
        subheading(styles, "标签解释", "Interpretation"),
        p(styles, "标签描述审阅者在该时间区间看到的主要表面重构类别。unknown 表示无法可靠判断。不要用模型 argmax 自动覆盖人的选择。", "Labels describe the dominant surface-reconstruction category seen by the reviewer over that interval. Use unknown when a reliable decision is not possible. Never overwrite the human choice with model argmax."),
        callout(styles, "显示增强不是数据增强 / Display adjustment is not data modification", "亮度、对比度和放大只帮助观察。导出会记录显示设置，但不会改写归档图像。", "Brightness, contrast, and zoom aid inspection only. Exports record display settings but do not rewrite archived images.", tone="info"),
        subheading(styles, "建议审阅节奏", "Recommended review rhythm"),
        table(styles, [
            ["阶段 / Stage", "操作 / Action"],
            ["粗看 / Survey", "快速拖动全程，识别明显转变和数据缺口"],
            ["精标 / Boundary pass", "逐帧确认 In/Out，并看 metadata"],
            ["复查 / Review", "检查未覆盖区、重叠错误、unknown 和备注"],
            ["保存 / Save", "导出 JSON，随后运行验证"],
        ], [42 * mm, CONTENT_WIDTH - 42 * mm]),
    ])

    # Page 9 - export validate
    page_break(story)
    story.extend([
        heading(styles, "8", "导出、导入与 fail-closed 验证", "Export, import, and fail-closed validation"),
        subheading(styles, "JSON 是 canonical 文件", "JSON is the canonical artifact"),
        p(styles, "Export JSON 用于继续标注、验证和下游处理；Export CSV 便于会议和表格分析，但不能替代 JSON 的完整嵌套溯源。", "Use Export JSON for resume, validation, and downstream processing. Export CSV is convenient for meetings and tabular analysis, but it does not replace the complete nested provenance in JSON."),
        table(styles, [
            ["绑定字段 / Binding", "作用 / Purpose"],
            ["dataset_id + source_archive_sha256", "绑定原始会话 / Bind the source session"],
            ["ordered_frame_fingerprint", "绑定帧顺序与逐帧 SHA / Bind order and per-frame SHA"],
            ["model_review_context_fingerprint", "绑定审阅时可见的 predictions/specs / Bind visible model context"],
            ["endpoint provenance", "绑定 frame ordinal、heartbeat、UTC、sequence 和 SHA / Bind each boundary"],
            ["model_outputs_visible=true", "披露模型可见 / Disclose model visibility"],
            ["eligible_for_gold=false", "禁止误当 blind-gold / Prevent blind-gold misuse"],
        ], [62 * mm, CONTENT_WIDTH - 62 * mm]),
        subheading(styles, "在桌面标注器验证", "Validate in the desktop labeler"),
        step(styles, 1, "选择原始 interactive_report.html。", "Select the original interactive_report.html."),
        step(styles, 2, "选择刚导出的 annotation JSON。", "Select the exported annotation JSON."),
        step(styles, 3, "点击 Validate annotations；只有显示 validation passed 才进入下一步。", "Click Validate annotations. Continue only after validation passes."),
        subheading(styles, "PowerShell 验证", "PowerShell validation"),
        code(styles, "python -m tools.rheed_postprocessing_labeling validate `\n  --report '<local-data-root>\\labeling-output\\interactive_report.html' `\n  --annotations '<local-data-root>\\exports\\segment_annotations.json'"),
        callout(styles, "验证失败不要绕过 / Never bypass a failure", "wrong run、模型上下文变化、端点被改、重叠或 gold 声明都会失败。回到正确的报告和原始导出重新核对。", "A wrong run, changed model context, altered endpoint, overlap, or gold claim all fail validation. Return to the correct report and original export and investigate.", tone="danger"),
    ])

    # Page 10 - safety
    page_break(story)
    story.extend([
        heading(styles, "9", "数据安全与科学边界", "Data safety and scientific boundaries"),
        subheading(styles, "仓库中允许和禁止的内容", "What belongs in Git"),
        table(styles, [
            ["可进入 Git / May be tracked", "不得默认进入 Git / Keep out by default"],
            ["工具代码、模板、测试、说明文档", "真实 session ZIP、原始 RHEED 图、sensor logs"],
            ["model_spec.example.json", "真实 predictions、生成报告、标注 JSON/CSV"],
            ["合成测试图和本手册示意图", "checkpoint、未公开结果、浏览器截图"],
        ], [CONTENT_WIDTH / 2, CONTENT_WIDTH / 2]),
        subheading(styles, "安全处理顺序", "Safe handling sequence"),
        step(styles, 1, "把原始 session ZIP 设为只读或保留不可修改副本，并记录 SHA-256。", "Keep the source session ZIP read-only or preserve an immutable copy, and record its SHA-256."),
        step(styles, 2, "在仓库外创建专用工作目录；报告、导出和截图都留在这里。", "Create a dedicated working directory outside the repository. Keep reports, exports, and screenshots there."),
        step(styles, 3, "交接时复制完整报告目录和 canonical JSON，并在外部单独保留原始 prediction CSV、model-spec JSON 与 SHA 清单；不要只发 HTML。", "For handoff, copy the whole report directory and canonical JSON, and separately preserve the original prediction CSV, model-spec JSON, and SHA manifest; never send only the HTML."),
        step(styles, 4, "验证通过后再进行统计或训练数据整理，并保留原始导出不变。", "Run statistics or training-data preparation only after validation, and preserve the original export unchanged."),
        callout(styles, "不是 blind-gold / Not blind-gold", "标注时模型曲线和 lossy review image 可见，所以这些标签只能作为 model-assisted review。正式 blind-gold 必须使用隐藏模型输出的独立流程。", "Model plots and lossy review images are visible during labeling, so these labels are model-assisted review only. Formal blind-gold data requires a separate workflow that hides model outputs.", tone="danger"),
        callout(styles, "不要扩大结论 / Do not over-interpret", "当前分类目标是裸 STO 表面重构。不要把结果直接解释为 FeSe 薄膜质量，也不要把 reconstruction unknown 等同于 QC reject。", "The current classification target is bare STO surface reconstruction. Do not directly interpret it as FeSe film quality, and do not equate reconstruction unknown with QC reject.", tone="warn"),
    ])

    # Page 11 - troubleshooting
    page_break(story)
    story.extend([
        heading(styles, "10", "故障排查", "Troubleshooting"),
        p(styles, "先保存错误文本、当前 branch/commit 和所选文件路径。不要用删除环境、全局变量或强制 Git 重置作为第一反应。若出现 AI4MBE live-driver warning，请记录缺失模块清单和启动器日志；dummy 及不受影响的模式仍可使用，但修复环境前不得用缺失驱动 ARM 生产会话。这是预期的可见告警，不应临时安装依赖来绕过。", "First preserve the exact error text, branch/commit, and selected paths. Do not begin by deleting environments, changing global variables, or force-resetting Git. If AI4MBE live-driver warning appears, record its missing-module list and launcher log. Dummy and unaffected modes remain available, but do not ARM a production session that needs a missing driver until the environment is repaired. This is expected fail-visible behavior, not a prompt for ad-hoc installation."),
        table(styles, [
            ["症状 / Symptom", "可能原因 / Likely cause", "安全操作 / Safe action"],
            ["桌面快捷方式缺失或指向旧目录", "安装脚本未运行或 checkout 已移动", "从当前仓库根目录运行 Install AI4MBE Desktop Shortcuts.cmd"],
            ["Growth Monitor 启动后 chamber 不对", "用了错误入口", "正常关闭；只用 Start Ch-MBE Growth Monitor.cmd"],
            ["启动窗口立即消失", "环境或依赖错误", "查看 %LOCALAPPDATA%\\AI4MBE\\LauncherLogs 最新日志；必要时从 PowerShell 运行同一 .cmd"],
            ["温度/RHEED 无读数", "仪器接口、vendor 窗口或模式问题", "记录 connected/error/mode；不要随意改全局环境变量"],
            ["标注器拒绝 Build", "输入缺失、pair 不匹配或输出目录非空", "逐项核对 Session、每行 pair 和新输出目录"],
            ["构建提示 provenance mismatch", "预测不属于该 ZIP 或行被改", "找回对应预测与 spec；不要编辑 CSV 绕过"],
            ["HTML 无图或曲线", "只复制了 HTML 或本地 asset 缺失", "恢复完整 report 目录；保持相对路径"],
            ["草稿消失", "换浏览器/电脑或清理 localStorage", "导入最近一次 JSON；以后更频繁导出"],
            ["Import/Validate 失败", "run/context/endpoint/overlap 不一致", "使用原始报告和原始 JSON，查看具体 fail-closed 信息"],
        ], [40 * mm, 58 * mm, CONTENT_WIDTH - 98 * mm]),
        Spacer(1, 3 * mm),
        subheading(styles, "需要提供给维护者的最小证据", "Minimum evidence for the maintainer"),
        bullet(styles, "错误信息全文和发生时间；不要只发“不能用”。", "Full error text and occurrence time; do not send only 'it does not work'."),
        bullet(styles, "git status --short --branch 与 git rev-parse HEAD。", "git status --short --branch and git rev-parse HEAD."),
        bullet(styles, "启动器名称、环境路径、输入文件名和输出目录；敏感数据不要发到公共频道。", "Launcher name, environment path, input filenames, and output directory; keep sensitive data out of public channels."),
        bullet(styles, "标注问题附 report manifest SHA 和 annotation JSON SHA，不需要先发送原始图。", "For labeling issues, provide report-manifest and annotation-JSON SHA values; raw images are not initially required."),
    ])

    # Page 12 - version and quick checklist
    page_break(story)
    story.extend([
        heading(styles, "11", "版本核对与一页检查表", "Version verification and one-page checklist"),
        subheading(styles, "实验或标注前记录版本", "Record versions before acquisition or labeling"),
        code(styles, "Set-Location '<GUI repository>'\ngit status --short --branch\ngit rev-parse HEAD\nconda env list\nconda activate ai4mbe-gui\npython --version\npython -m tools.rheed_postprocessing_labeling --help"),
        p(styles, "GUI 的实际 Python 和仓库路径以启动器日志为准；AI4MBE_GUI_PYTHON 仅在该工作站已配置时读取，不要临时创建。若 branch/commit 与团队指定版本不同，或 git status 显示未知改动，先停止并核对。", "Use the resolved Python and repository paths printed by the launcher log. Read AI4MBE_GUI_PYTHON only when it is already configured on that workstation; do not create it ad hoc. If branch/commit differs from the team-specified version, or git status shows unknown changes, stop and verify."),
        subheading(styles, "完整性哈希", "Integrity hashes"),
        code(styles, "Get-FileHash '<local-data-root>\\growth_session.zip' -Algorithm SHA256\nGet-FileHash '<local-data-root>\\exports\\segment_annotations.json' -Algorithm SHA256\nGet-FileHash '.\\docs\\RHEED_GUI_Postprocessing_Labeling_User_Manual.pdf' -Algorithm SHA256"),
        subheading(styles, "快速检查表", "Quick checklist"),
        table(styles, [
            ["□", "操作员确认 / Operator confirmation"],
            ["□", "使用 Start Ch-MBE Growth Monitor.cmd，chamber 显示正确"],
            ["□", "实时状态、帧 sequence、温度和日志行为符合实验 SOP"],
            ["□", "原始 session ZIP 有只读副本和 SHA-256"],
            ["□", "用 Start RHEED Post-processing Labeler.cmd 打开离线工具"],
            ["□", "每个 prediction CSV 与 model-spec JSON 正确配对"],
            ["□", "输出目录在 Git 外且为新目录或空目录"],
            ["□", "时间分段无重叠，边界逐帧核对，unknown/notes 使用合理"],
            ["□", "Export JSON 后使用原始 interactive_report.html 验证通过"],
            ["□", "记录 branch、commit、环境、报告 manifest 和导出 SHA"],
            ["□", "交接完整 report 目录；声明 model-assisted、not blind-gold"],
        ], [10 * mm, CONTENT_WIDTH - 10 * mm]),
        Spacer(1, 4 * mm),
        callout(styles, "停止条件 / Stop condition", "出现无法解释的仪器状态、数据旧值、版本不明、provenance mismatch 或验证失败时，停止继续采集/标注并联系维护者。", "Stop and contact the maintainer if instrument state, stale data, version identity, provenance, or validation cannot be explained.", tone="danger"),
        Spacer(1, 5 * mm),
        HRFlowable(width="100%", thickness=0.7, color=LINE),
        Spacer(1, 3 * mm),
        p(styles, "本手册中的界面和 RHEED 图均为合成示意，不包含真实实验数据。", "All interface and RHEED images in this manual are synthetic illustrations and contain no experimental data.", "small"),
    ])
    return story


def generate_pdf() -> Path:
    register_fonts()
    assets = generate_assets()
    OUTPUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    styles = make_styles()
    document = SimpleDocTemplate(
        str(OUTPUT_PDF),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=17 * mm,
        title="Ch-MBE GUI and RHEED Post-processing Labeling User Manual",
        author="AI4MBE",
    )
    document.build(build_story(styles, assets), canvasmaker=ManualCanvas)
    return OUTPUT_PDF


if __name__ == "__main__":
    output = generate_pdf()
    print(output)
