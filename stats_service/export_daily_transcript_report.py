from __future__ import annotations

import argparse
import asyncio
import html
import os
import re
import socket
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, A3, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    PageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


REPORT_COLUMNS = [
    "created_at",
    "recognition_text",
    "seller_id",
    "client_id",
    "dialog_type",
    "is_sale",
    "is_alarm_triggered",
    "session_id",
]


@dataclass(frozen=True)
class ReportRow:
    session_id: str
    store_id: str | None
    client_id: str | None
    seller_id: str | None
    recognition_text: str | None
    dialog_type: str | None
    is_sale: bool
    is_alarm_triggered: bool
    created_at: datetime

    @classmethod
    def from_record(cls, record: dict) -> "ReportRow":
        return cls(
            session_id=str(record.get("session_id") or ""),
            store_id=record.get("store_id"),
            client_id=record.get("client_id"),
            seller_id=record.get("seller_id"),
            recognition_text=record.get("recognition_text"),
            dialog_type=record.get("dialog_type"),
            is_sale=bool(record.get("is_sale", False)),
            is_alarm_triggered=bool(record.get("is_alarm_triggered", False)),
            created_at=record["created_at"],
        )


@dataclass
class SheetSpec:
    name: str
    headers: list[str]
    rows: list[list[object]]
    wrap_columns: set[int] | None = None
    datetime_columns: set[int] | None = None
    freeze_panes: str = "A2"
    auto_filter: bool = True


def excel_column_name(index: int) -> str:
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def escape_xml(text: str) -> str:
    return html.escape(text, quote=True)


def to_excel_serial(value: datetime) -> float:
    base = datetime(1899, 12, 30)
    naive_value = value.replace(tzinfo=None)
    return (naive_value - base).total_seconds() / 86400.0


def normalize_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def safe_sheet_name(name: str) -> str:
    invalid = '[]:*?/\\'
    cleaned = "".join("_" if char in invalid else char for char in name).strip()
    if not cleaned:
        cleaned = "Sheet"
    return cleaned[:31]


def cell_xml(row_index: int, column_index: int, value: object, style_id: int = 0) -> str:
    ref = f"{excel_column_name(column_index)}{row_index}"

    if value is None or value == "":
        return ""

    if isinstance(value, bool):
        return f'<c r="{ref}" t="inlineStr" s="{style_id}"><is><t>{escape_xml(normalize_value(value))}</t></is></c>'

    if isinstance(value, datetime):
        serial = to_excel_serial(value)
        return f'<c r="{ref}" s="{style_id}"><v>{serial:.8f}</v></c>'

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style_id}"><v>{value}</v></c>'

    text = escape_xml(str(value))
    return (
        f'<c r="{ref}" t="inlineStr" s="{style_id}">'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def build_sheet_xml(sheet: SheetSpec) -> str:
    max_column = len(sheet.headers)
    max_row = len(sheet.rows) + 1
    last_ref = f"{excel_column_name(max_column)}{max_row}"

    row_xml = []

    header_cells = []
    for column_index, header in enumerate(sheet.headers, start=1):
        header_cells.append(cell_xml(1, column_index, header, style_id=1))
    row_xml.append(f'<row r="1" spans="1:{max_column}">{"".join(header_cells)}</row>')

    wrap_columns = sheet.wrap_columns or set()
    datetime_columns = sheet.datetime_columns or set()

    for row_index, row in enumerate(sheet.rows, start=2):
        cells = []
        for column_index, value in enumerate(row, start=1):
            style_id = 0
            if column_index in datetime_columns:
                style_id = 2
            elif column_index in wrap_columns:
                style_id = 3
            cell = cell_xml(row_index, column_index, value, style_id=style_id)
            if cell:
                cells.append(cell)
        row_xml.append(
            f'<row r="{row_index}" spans="1:{max_column}">{"".join(cells)}</row>'
        )

    cols = []
    for column_index in range(1, max_column + 1):
        if column_index in wrap_columns:
            width = 60
        else:
            width = 18
        if column_index in datetime_columns:
            width = 21
        cols.append(
            f'<col min="{column_index}" max="{column_index}" width="{width}" customWidth="1"/>'
        )

    freeze_xml = ""
    if sheet.freeze_panes:
        match = re.fullmatch(r"([A-Z]+)(\d+)", sheet.freeze_panes)
        split_row = 1
        top_left_cell = sheet.freeze_panes
        if match:
            split_row = max(int(match.group(2)) - 1, 0)
            top_left_cell = sheet.freeze_panes
        freeze_xml = (
            "<sheetViews><sheetView workbookViewId=\"0\">"
            f"<pane ySplit=\"{split_row}\" topLeftCell=\"{top_left_cell}\" activePane=\"bottomLeft\" state=\"frozen\"/>"
            "</sheetView></sheetViews>"
        )

    auto_filter_xml = ""
    if sheet.auto_filter:
        auto_filter_xml = f'<autoFilter ref="A1:{last_ref}"/>'

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"{freeze_xml}"
        f"<dimension ref=\"A1:{last_ref}\"/>"
        f"<sheetFormatPr defaultRowHeight=\"18\"/>"
        f"<cols>{''.join(cols)}</cols>"
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        f"{auto_filter_xml}"
        "</worksheet>"
    )


def workbook_styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1">
    <numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/>
  </numFmts>
  <fonts count="3">
    <font>
      <sz val="11"/>
      <color theme="1"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <b/>
      <sz val="11"/>
      <color theme="1"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <sz val="11"/>
      <color theme="1"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
  </fonts>
  <fills count="3">
    <fill>
      <patternFill patternType="none"/>
    </fill>
    <fill>
      <patternFill patternType="gray125"/>
    </fill>
    <fill>
      <patternFill patternType="solid">
        <fgColor rgb="FFD9EAF7"/>
        <bgColor indexed="64"/>
      </patternFill>
    </fill>
  </fills>
  <borders count="2">
    <border>
      <left/>
      <right/>
      <top/>
      <bottom/>
      <diagonal/>
    </border>
    <border>
      <left style="thin"><color rgb="FFD0D7DE"/></left>
      <right style="thin"><color rgb="FFD0D7DE"/></right>
      <top style="thin"><color rgb="FFD0D7DE"/></top>
      <bottom style="thin"><color rgb="FFD0D7DE"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyBorder="1"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1" applyBorder="1">
      <alignment vertical="center" horizontal="center" wrapText="1"/>
    </xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1">
      <alignment vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1" applyBorder="1">
      <alignment vertical="top" wrapText="1"/>
    </xf>
  </cellXfs>
  <cellStyles count="1">
    <cellStyle name="Normal" xfId="0" builtinId="0"/>
  </cellStyles>
</styleSheet>
"""


def workbook_xml(sheet_names: list[str]) -> str:
    sheets_xml = []
    for index, name in enumerate(sheet_names, start=1):
        sheets_xml.append(
            f'<sheet name="{escape_xml(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(sheets_xml)}</sheets>"
        "</workbook>"
    )


def workbook_rels_xml(sheet_count: int) -> str:
    rels = []
    for index in range(1, sheet_count + 1):
        rels.append(
            f'<Relationship Id="rId{index}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )

    rels.append(
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(rels)}"
        "</Relationships>"
    )


def root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="xl/workbook.xml"/>
</Relationships>
"""


def content_types_xml(sheet_count: int) -> str:
    overrides = []
    for index in range(1, sheet_count + 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{''.join(overrides)}"
        "</Types>"
    )


def build_sheets(
    rows: list[ReportRow],
    report_date: date,
    generated_at: datetime,
    tz_name: str,
) -> list[SheetSpec]:

    zone = ZoneInfo(tz_name)

    stores: dict[str, list[ReportRow]] = {}

    for row in rows:
        store_name = row.store_id or ""
        stores.setdefault(store_name, []).append(row)

    sheets = []

    for store_id, store_rows in sorted(stores.items()):

        store_rows = sorted(
            store_rows,
            key=lambda row: row.created_at
        )

        sheet_rows = [
            [
                row.created_at.astimezone(zone),
                row.recognition_text or "",
                row.seller_id or "",
                row.client_id or "",
                row.dialog_type or "",
                row.is_sale,
                row.is_alarm_triggered,
                row.session_id,
            ]
            for row in store_rows
        ]

        sheet_name = store_id if store_id else "Без магазина"

        sheets.append(
            SheetSpec(
                name=safe_sheet_name(sheet_name),
                headers=REPORT_COLUMNS,
                rows=sheet_rows,
                wrap_columns={2},
                datetime_columns={1},
            )
        )

    return sheets


def write_xlsx(path: Path, sheets: list[SheetSpec]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    sheet_names = [safe_sheet_name(sheet.name) for sheet in sheets]
    sheet_xmls = [build_sheet_xml(sheet) for sheet in sheets]

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", root_rels_xml())
        archive.writestr("xl/workbook.xml", workbook_xml(sheet_names))
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        archive.writestr("xl/styles.xml", workbook_styles_xml())
        for index, sheet_xml in enumerate(sheet_xmls, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml)


async def fetch_rows(
    dsn: str | None,
    start_at: datetime,
    end_at: datetime,
) -> list[ReportRow]:
    import asyncpg

    if dsn:
        pool = await asyncpg.create_pool(dsn)
        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(
                    """
                    SELECT
                        session_id,
                        store_id,
                        client_id,
                        seller_id,
                        recognition_text,
                        dialog_type,
                        is_sale,
                        is_alarm_triggered,
                        created_at
                    FROM transcripts
                    WHERE created_at >= $1
                      AND created_at < $2
                    ORDER BY created_at, session_id
                    """,
                    start_at,
                    end_at,
                )
        finally:
            await pool.close()
    else:
        records = []
        conn = await asyncpg.connect(
            host=env_value("POSTGRES_HOST", "localhost"),
            port=int(env_value("POSTGRES_PORT", "5432")),
            user=env_value("POSTGRES_USER", "speech"),
            password=env_value("POSTGRES_PASSWORD", "speech"),
            database=env_value("POSTGRES_DB", "speech_db"),
        )
        try:
            records = await conn.fetch(
                """
                SELECT
                    session_id,
                    store_id,
                    client_id,
                    seller_id,
                    recognition_text,
                    dialog_type,
                    is_sale,
                    is_alarm_triggered,
                    created_at
                FROM transcripts
                WHERE created_at >= $1
                  AND created_at < $2
                ORDER BY created_at, session_id
                """,
                start_at,
                end_at,
            )
        finally:
            await conn.close()

    return [ReportRow.from_record(dict(record)) for record in records]


def resolve_target_date(date_value: str | None, tz_name: str) -> date:
    zone = ZoneInfo(tz_name)
    if date_value:
        return datetime.strptime(date_value, "%Y-%m-%d").date()
    return datetime.now(zone).date() - timedelta(days=1)


def build_output_path(output: str | None, report_date: date) -> Path:
    if output:
        return Path(output)

    output_dir = Path(
        os.getenv(
            "REPORT_OUTPUT_DIR",
            "reports"
        )
    )

    return output_dir / (
        f"transcript_report_{report_date.isoformat()}.xlsx"
    )


def build_pdf_output_path(output: str | None, report_date: date) -> Path:
    if output:
        return Path(output).with_suffix(".pdf")

    output_dir = Path(
        os.getenv(
            "REPORT_OUTPUT_DIR",
            "reports"
        )
    )

    return output_dir / (
        f"transcript_report_{report_date.isoformat()}.pdf"
    )

def env_value(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default

def write_pdf(
    path: Path,
    rows: list[ReportRow],
    report_date: date,
    generated_at: datetime,
    tz_name: str,
) -> None:

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

    if Path(font_path).exists():
        pdfmetrics.registerFont(
            TTFont("DejaVuSans", font_path)
        )
        font_name = "DejaVuSans"
    else:
        font_name = "Helvetica"

    print("PDF FONT:", font_name)


    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A3),
        rightMargin=15,
        leftMargin=15,
        topMargin=20,
        bottomMargin=20,
    )

    normal = ParagraphStyle(
        "normal",
        fontName=font_name,
        fontSize=8,
        leading=10,
    )

    def pdf_cell(value):
        return Paragraph(
            html.escape(str(value or "")),
            normal
        )

    header = ParagraphStyle(
        "header",
        fontName=font_name,
        fontSize=12,
        leading=14,
    )


    story = []


    stores: dict[str, list[ReportRow]] = {}

    for row in rows:
        store = row.store_id or ""
        stores.setdefault(store, []).append(row)


    for store_id, store_rows in sorted(stores.items()):

        store_rows.sort(
            key=lambda r: r.created_at
        )


        story.append(
            Paragraph(
                f"Магазин: {store_id}",
                header
            )
        )

        story.append(Spacer(1, 10))


        table_data = [
            [
                "Время",
                "Расшифровка",
                "Продавец",
                "Клиент",
                "Тип",
                "Успех",
                "Тревога",
                "Session",
            ]
        ]


        for row in store_rows:

            table_data.append(
                [
                    pdf_cell(row.created_at.strftime("%Y-%m-%d %H:%M:%S")),
                    pdf_cell(row.recognition_text),
                    pdf_cell(row.seller_id),
                    pdf_cell(row.client_id),
                    pdf_cell(row.dialog_type),
                    pdf_cell("Да" if row.is_sale else ""),
                    pdf_cell("Да" if row.is_alarm_triggered else ""),
                    pdf_cell(row.session_id),
                ]
            )


        table = Table(
            table_data,
            repeatRows=1,
            colWidths=[
                90,    # время
                550,   # расшифровка
                80,    # продавец
                40,    # клиент
                30,    # тип
                40,    # успех
                40,    # тревога
                220,   # session
            ]
        )


        table.setStyle(
            TableStyle(
                [
                    (
                        "FONT",
                        (0,0),
                        (-1,-1),
                        font_name,
                        8,
                    ),
                    (
                        "BACKGROUND",
                        (0,0),
                        (-1,0),
                        colors.lightgrey,
                    ),
                    (
                        "VALIGN",
                        (0,0),
                        (-1,-1),
                        "TOP",
                    ),
                    (
                        "GRID",
                        (0,0),
                        (-1,-1),
                        0.25,
                        colors.grey,
                    ),
                    (
                        "LEFTPADDING",
                        (0,0),
                        (-1,-1),
                        4,
                    ),
                    (
                        "RIGHTPADDING",
                        (0,0),
                        (-1,-1),
                        4,
                    ),
                ]
            )
        )


        story.append(table)
        story.append(PageBreak())


    doc.build(story)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export daily transcript rows from PostgreSQL into a readable XLSX report."
    )
    parser.add_argument(
        "--date",
        help="Report date in YYYY-MM-DD format. Default: yesterday in the chosen timezone.",
    )
    parser.add_argument(
        "--timezone",
        default=os.getenv("REPORT_TIMEZONE", "Europe/Moscow"),
        help="Timezone used for the date boundary and workbook timestamps.",
    )
    parser.add_argument(
        "--output",
        help="Output .xlsx file path. Default: transcript_report_<date>.xlsx",
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("POSTGRES_DSN"),
        help="Optional PostgreSQL DSN. If omitted, uses POSTGRES_* environment variables.",
    )

    args = parser.parse_args()

    report_date = resolve_target_date(args.date, args.timezone)
    zone = ZoneInfo(args.timezone)

    start_at = datetime.combine(report_date, time.min, tzinfo=zone)
    end_at = start_at + timedelta(days=1)

    try:
        rows = await fetch_rows(args.dsn, start_at, end_at)
    except (socket.gaierror, OSError) as exc:
        host = env_value("POSTGRES_HOST", "localhost")
        port = env_value("POSTGRES_PORT", "5432")
        raise SystemExit(
            "Could not connect to PostgreSQL. "
            f"Current host is {host}:{port}. "
            "If you run the database locally, set POSTGRES_HOST=localhost. "
            "If you run the script inside Docker, set POSTGRES_HOST=postgres. "
            f"Original error: {exc}"
        ) from exc
    generated_at = datetime.now(zone)
    sheets = build_sheets(rows, report_date, generated_at, args.timezone)
    output_path = build_output_path(args.output, report_date)
    pdf_output_path = build_pdf_output_path(args.output, report_date)

    write_xlsx(output_path, sheets)
    write_pdf(pdf_output_path, rows, report_date, generated_at, args.timezone)

    print(f"Wrote {output_path.resolve()}")
    print(f"Wrote {pdf_output_path.resolve()}")
    print(f"Rows exported: {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
