from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import pandas as pd


@dataclass
class Row:
    created_at: datetime
    text: str
    seller_id: str | None
    client_id: str | None
    dialog_type: str | None
    is_sale: bool
    is_alarm_triggered: bool
    session_id: str | None
    index: int


@dataclass
class Block:
    store_id: str
    seller_id: str | None
    rows: list[Row]
    client_id_hint: str | None  # существующий client_id блока (если есть)

    @property
    def start(self) -> datetime:
        return self.rows[0].created_at

    @property
    def end(self) -> datetime:
        return self.rows[-1].created_at

    @property
    def duration_sec(self) -> float:
        return float((self.end - self.start).total_seconds())

    @property
    def text(self) -> str:
        parts = [r.text.strip() for r in self.rows if r.text and r.text.strip()]
        return "\n".join(parts)

    @property
    def session_ids(self) -> list[str]:
        result: list[str] = []
        seen = set()
        for r in self.rows:
            if r.session_id and r.session_id not in seen:
                seen.add(r.session_id)
                result.append(r.session_id)
        return result

    @property
    def is_sale_any(self) -> bool:
        return any(r.is_sale for r in self.rows)

    @property
    def alarm_any(self) -> bool:
        return any(r.is_alarm_triggered for r in self.rows)

    @property
    def dialog_types(self) -> list[str]:
        result: list[str] = []
        for r in self.rows:
            if r.dialog_type and r.dialog_type not in result:
                result.append(r.dialog_type)
        return result


def load_raw(xlsx_path: str) -> list[tuple[str, pd.DataFrame]]:
    """Считывает сырой дневной файл: [(store_id, df)], df отсортирован по времени.

    Файлы-заглушки (пустой workbook без листов) -> [].
    """
    try:
        sheets = pd.read_excel(xlsx_path, sheet_name=None)
    except ValueError:
        return []
    result = []
    for store_id, df in sheets.items():
        if df is None or len(df) == 0:
            continue
        df = df.copy()
        if "created_at" in df.columns:
            df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
        df = df.dropna(subset=["created_at"])
        df = df.sort_values("created_at", kind="stable").reset_index(drop=True)
        result.append((store_id, df))
    return result


def df_to_rows(df: pd.DataFrame) -> list[Row]:
    def _s(v) -> str | None:
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        s = str(v).strip()
        return s if s and s.lower() != "nan" else None

    def _b(v) -> bool:
        if isinstance(v, bool):
            return v
        s = _s(v)
        return s is not None and s.lower() in ("true", "1", "yes", "y", "t")

    rows: list[Row] = []
    for i, rec in enumerate(df.itertuples(index=False)):
        d = dict(zip(["created_at", "recognition_text", "seller_id", "client_id", "dialog_type", "is_sale", "is_alarm_triggered", "session_id"], rec))
        created = pd.Timestamp(d["created_at"])
        if created is None or pd.isna(created):
            continue
        rows.append(
            Row(
                created_at=created.to_pydatetime(),
                text=_s(d["recognition_text"]) or "",
                seller_id=_s(d["seller_id"]),
                client_id=_s(d["client_id"]),
                dialog_type=_s(d["dialog_type"]),
                is_sale=_b(d["is_sale"]),
                is_alarm_triggered=_b(d["is_alarm_triggered"]),
                session_id=_s(d["session_id"]),
                index=i,
            )
        )
    return rows


PREAMBLE = "__PREAMBLE__"


def _run_ranges(labels: Sequence[str]) -> list[tuple[int, int]]:
    """Непрерывные пролеты одинаковых меток."""
    runs: list[tuple[str, int, int]] = []
    i = 0
    L = len(labels)
    while i < L:
        j = i
        while j + 1 < L and labels[j + 1] == labels[i]:
            j += 1
        runs.append((labels[i], i, j))
        i = j + 1
    return [(a, b + 1) for (_, a, b) in runs]


def build_blocks(rows: list[Row], store_id: str) -> list[Block]:
    """Делит поток реплик магазина на блоки-кандидаты.

    Один блок = непрерывный пролёт одного (forward-fill-ованного) client_id.
    Это соответствует эталонному отчёту: 1 клиентский разговор = 1 строка.
    Реплики без client_id (до первого клиента) образуют отдельный preamble-пролёт.
    """
    L = len(rows)
    if L == 0:
        return []

    if any(r.client_id for r in rows):
        filled: list[str | None] = []
        current = None
        first_client: str | None = None
        for r in rows:
            if r.client_id:
                current = r.client_id
                if first_client is None:
                    first_client = current
            filled.append(current)
        # BUG-1 fix: реплики ПЕРЕД первым client_id (realtime фиксирует климента
        # только после «Здравствуйте») относятся к тому же клиентскому разговору —
        # заполняем их первым реальным client_id, не создавая "offline_..." блок.
        if first_client is not None:
            for i in range(L):
                if filled[i] is None:
                    filled[i] = first_client
                else:
                    break
    else:
        filled = [None] * L

    labels = [c if c is not None else PREAMBLE for c in filled]

    blocks: list[Block] = []
    for (s, e) in _run_ranges(labels):
        lab = labels[s]
        chunk = rows[s:e]
        if not chunk:
            continue
        client_hint = None if lab == PREAMBLE else lab
        blocks.append(
            Block(store_id=store_id, seller_id=chunk[0].seller_id, rows=list(chunk), client_id_hint=client_hint)
        )
    return blocks
