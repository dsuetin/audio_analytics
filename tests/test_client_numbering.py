from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

from stats_service.client_numbering import assign_display_client_ids
from classification.state import StateManager
from classification.dialog_sessions import DialogSession


# ---------------------------------------------------------------------------
# 1-3. Последовательная нумерация внутри каждого магазина
# ---------------------------------------------------------------------------

def test_single_store_sequential_numbering():
    rows = [
        {"store_id": "A", "client_id": "client_7"},
        {"store_id": "A", "client_id": "client_9"},
        {"store_id": "A", "client_id": "client_12"},
    ]
    out = assign_display_client_ids(rows)
    assert [r["client_id"] for r in out] == ["client_1", "client_2", "client_3"]
    assert [r["internal_client_id"] for r in out] == ["client_7", "client_9", "client_12"]


def test_two_stores_each_start_from_one():
    rows = [
        {"store_id": "A", "client_id": "client_5"},
        {"store_id": "A", "client_id": "client_6"},
        {"store_id": "B", "client_id": "client_11"},
        {"store_id": "B", "client_id": "client_13"},
    ]
    out = assign_display_client_ids(rows)
    per_store = {}
    for r in out:
        per_store.setdefault(r["store_id"], []).append(r["client_id"])
    assert per_store["A"] == ["client_1", "client_2"]
    assert per_store["B"] == ["client_1", "client_2"]


def test_three_stores_independent_counting():
    rows = []
    for store, n in (("A", 3), ("B", 2), ("C", 1)):
        for i in range(n):
            rows.append({"store_id": store, "client_id": f"client_{i + 100}"})
    out = assign_display_client_ids(rows)
    per_store = {}
    for r in out:
        per_store.setdefault(r["store_id"], []).append(r["client_id"])
    assert per_store["A"] == ["client_1", "client_2", "client_3"]
    assert per_store["B"] == ["client_1", "client_2"]
    assert per_store["C"] == ["client_1"]


def test_same_client_rows_keep_same_id_and_order_preserved():
    rows = [
        {"store_id": "A", "client_id": "client_15"},
        {"store_id": "A", "client_id": "client_15"},
        {"store_id": "A", "client_id": "client_17"},
    ]
    out = assign_display_client_ids(rows)
    assert [r["client_id"] for r in out] == ["client_1", "client_1", "client_2"]


def test_rows_without_client_id_get_none_internal():
    rows = [
        {"store_id": "A", "client_id": None},
        {"store_id": "A", "client_id": "client_21"},
    ]
    out = assign_display_client_ids(rows)
    assert out[0]["client_id"] is None
    assert out[0]["internal_client_id"] is None
    assert out[1]["client_id"] == "client_1"
    assert out[1]["internal_client_id"] == "client_21"


def test_same_internal_id_is_consistently_mapped():
    # один и тот же internal ID у всех строк клиента получает ОДИН display-номер;
    # разные internal ID в том же магазине — разные номера
    rows = [
        {"store_id": "A", "client_id": "client_1"},
        {"store_id": "A", "client_id": "client_1"},
        {"store_id": "A", "client_id": "client_2"},
    ]
    out = assign_display_client_ids(rows)
    assert [r["client_id"] for r in out] == ["client_1", "client_1", "client_2"]
    assert [r["internal_client_id"] for r in out] == ["client_1", "client_1", "client_2"]


# ---------------------------------------------------------------------------
# Offline-анализ: run.build_final_rows даёт display-номера и сохраняет internal
# ---------------------------------------------------------------------------

def _make_block(store_id, internal_client_id, start_time):
    from datetime import timedelta
    from offline_analysis.segmentation import Row, Block

    base = start_time
    rows = [
        Row(
            created_at=base,
            text="здравствуйте, что ищете",
            seller_id="ivan",
            client_id=None,
            dialog_type=None,
            is_sale=False,
            is_alarm_triggered=False,
            session_id="s1",
            index=0,
        ),
        Row(
            created_at=base + timedelta(seconds=5),
            text="хочу посмотреть, потом до свидания",
            seller_id="ivan",
            client_id=internal_client_id,
            dialog_type="buy",
            is_sale=True,
            is_alarm_triggered=False,
            session_id="s2",
            index=1,
        ),
    ]
    return Block(
        store_id=store_id,
        seller_id="ivan",
        rows=rows,
        client_id_hint=internal_client_id,
    )


def test_offline_build_final_rows_numbering_per_store():
    from datetime import datetime, timedelta
    from offline_analysis.run import build_final_rows

    base = datetime(2026, 8, 27, 10, 0, 0)
    blocks = [
        _make_block("storeA", "client_17", base),
        _make_block("storeA", "client_19", base + timedelta(hours=1)),
        _make_block("storeB", "client_5", base + timedelta(minutes=5)),
        _make_block("storeB", "client_5", base + timedelta(hours=2)),
        # блок без client_id -> offline-technical ID (не должно ломать нумерацию)
        _make_block("storeC", None, base + timedelta(minutes=10)),
    ]

    results = [
        {
            "block_index": i,
            "role": "customer",
            "mission": "Купить",
            "is_sale": True,
            "loss_reason": None,
            "confidence": 0.9,
            "reason": "ok",
            "dialog_type": "buy",
            "raw": None,
        }
        for i in range(len(blocks))
    ]
    # технический ID для блока без client_id — как в run.py
    blocks[4].client_id_hint = None

    rows, _rejected = build_final_rows(results, blocks, confidence_min=0.5)

    per_store = {}
    for r in rows:
        per_store.setdefault(r["store_id"], []).append(
            (r["client_id"], r.get("internal_client_id"))
        )

    assert per_store["storeA"] == [
        ("client_1", "client_17"),
        ("client_2", "client_19"),
    ]
    # storeB: одиница ID (restarter) у двух разных разговоров ->
    # разные display-номера, не сливаются в один client_1
    assert per_store["storeB"] == [
        ("client_1", "client_5"),
        ("client_2", "client_5"),
    ]
    # storeC: offline-технический ID остался technical, display = client_1
    assert per_store["storeC"][0][0] == "client_1"
    assert per_store["storeC"][0][1].startswith("offline_")


def test_input_rows_are_not_mutated():
    rows = [{"store_id": "A", "client_id": "client_5"}]
    snapshot = dict(rows[0])
    out = assign_display_client_ids(rows)
    assert rows[0] == snapshot
    assert out[0]["internal_client_id"] == "client_5"
    assert out[0]["client_id"] == "client_1"


# ---------------------------------------------------------------------------
# 9. Технические ID не ломаются: realtime-счётчик по-прежнему на магазин,
#    и в рамках одного процесса всегда последовательный
# ---------------------------------------------------------------------------

class _FakeService:
    def __init__(self):
        self.state = StateManager()

    async def fake_emit(self):
        pass

    async def fake_save(self, session_id, client_id):
        pass

    async def new_client(self, store_id, session_id):
        store_state = self.state.store(store_id)
        client_id = f"client_{len(store_state.clients)+1}"
        store_state.current_client_id = client_id
        store_state.client(client_id)
        return client_id


def test_realtime_id_generator_is_per_store_and_sequential():
    import asyncio
    from unittest.mock import patch

    svc = _FakeService()

    async def scenario():
        # имитируем service.handle: new client -> id
        with patch("classification.state.ClientState"):
            pass
        assert await svc.new_client("A", "s1") == "client_1"
        assert await svc.new_client("A", "s2") == "client_2"
        assert await svc.new_client("B", "s3") == "client_1"
        # два магазина одновременно: не мешают друг другу
        assert await svc.new_client("A", "s4") == "client_3"
        assert await svc.new_client("B", "s5") == "client_2"

    asyncio.run(scenario())

    st_a = svc.state.store("A")
    st_b = svc.state.store("B")
    assert st_a.current_client_id == "client_3"
    assert st_b.current_client_id == "client_2"
    assert set(st_a.clients) == {"client_1", "client_2", "client_3"}
    assert set(st_b.clients) == {"client_1", "client_2"}


def test_new_client_after_farewell_then_greeting():
    dialog = DialogSession()
    assert dialog.process("что-то", True) is False
    assert dialog.process("до свидания", True) is False
    assert dialog.process("продолжаем", True) is False
    assert dialog.process("здравствуйте", True) is True


def test_two_greetings_in_a_row_do_not_create_new_client():
    dialog = DialogSession()
    assert dialog.process("здравствуйте", True) is True
    assert dialog.process("здравствуйте", True) is False
    assert dialog.process("здравствуйте", True) is False


def test_non_final_greeting_does_not_create_new_client():
    dialog = DialogSession()
    assert dialog.process("здравствуйте", False) is False
    assert dialog.process("здравствуйте", False) is False


# ---------------------------------------------------------------------------
# 6. Реплики до появления client_id остаются внутри одного блока клиента
# ---------------------------------------------------------------------------

def test_preamble_rows_merge_into_first_client_block():
    from datetime import datetime, timedelta
    from offline_analysis.segmentation import Row, build_blocks

    base = datetime(2026, 8, 27, 10, 0, 0)
    rows = [
        Row(base, "рассматриваю товар", "ivan", None, None, False, False, "s1", 0),
        Row(base + timedelta(seconds=5), "здравствуйте", "ivan", None, None, False, False, "s2", 1),
        Row(base + timedelta(seconds=10), "сколько стоит", "ivan", "client_15", "buy", False, False, "s3", 2),
        Row(base + timedelta(seconds=15), "хорошо", "ivan", "client_15", "buy", True, False, "s4", 3),
        Row(base + timedelta(seconds=20), "до свидания", "ivan", "client_15", "buy", True, False, "s5", 4),
    ]
    blocks = build_blocks(rows, "storeA")
    assert len(blocks) == 1
    assert blocks[0].client_id_hint == "client_15"
    assert len(blocks[0].rows) == 5


# ---------------------------------------------------------------------------
# Realtime: два продавца одного магазина — один счётчик магазина
# ---------------------------------------------------------------------------

def test_same_store_two_sellers_share_one_client_counter():
    manager = StateManager()
    store = manager.store("A")
    # имитация service.handle: новый клиент
    cid1 = f"client_{len(store.clients) + 1}"
    store.current_client_id = cid1
    store.client(cid1)

    cid2 = f"client_{len(store.clients) + 1}"
    store.current_client_id = cid2
    store.client(cid2)

    assert cid1 == "client_1"
    assert cid2 == "client_2"
    assert set(store.clients) == {"client_1", "client_2"}


def test_other_stores_do_not_affect_counter():
    manager = StateManager()
    store_a = manager.store("A")
    store_b = manager.store("B")

    for _ in range(3):
        cid = f"client_{len(store_a.clients) + 1}"
        store_a.client(cid)

    cid_b = f"client_{len(store_b.clients) + 1}"
    store_b.client(cid_b)

    assert list(store_a.clients) == ["client_1", "client_2", "client_3"]
    assert cid_b == "client_1"


# ---------------------------------------------------------------------------
# 10. Финальный Excel (реальная функция stats_service.create_final_report)
# ---------------------------------------------------------------------------

def _stub_optional_deps():
    """Заменяет heavy-зависимости (fastapi/uvicorn), которых нет в test-окружении."""
    if "fastapi" not in sys.modules:
        fastapi_stub = types.ModuleType("fastapi")

        class _FastAPI:
            def __init__(self, *args, **kwargs):
                pass

            def post(self, *args, **kwargs):
                def decorator(fn):
                    return fn
                return decorator

        fastapi_stub.FastAPI = _FastAPI
        sys.modules["fastapi"] = fastapi_stub
    if "uvicorn" not in sys.modules:
        sys.modules["uvicorn"] = types.ModuleType("uvicorn")


@pytest.fixture
def final_report_path(tmp_path):
    return tmp_path / "final.xlsx"


def test_create_final_report_numbering_per_store(tmp_path):
    _stub_optional_deps()

    raw = tmp_path / "raw.xlsx"
    with pd.ExcelWriter(raw, engine="openpyxl") as w:
        pd.DataFrame(
            {
                "created_at": [
                    "2026-08-27 10:00:00",
                    "2026-08-27 10:01:00",
                    "2026-08-27 10:02:00",
                    "2026-08-27 10:03:00",
                    "2026-08-27 10:04:00",
                    "2026-08-27 10:05:00",
                    "2026-08-27 10:06:00",
                    "2026-08-27 10:07:00",
                ],
                "recognition_text": [
                    "здравствуйте, что ищете",
                    "хочу посмотреть",
                    "до свидания",
                    "здравствуйте, снова",
                    "спасибо",
                    "здравствуйте",
                    "добрый день, нужна помощь",
                    "до свидания",
                ],
                "seller_id": ["ivan"] * 8,
                "client_id": [
                    None,
                    "client_5",
                    "client_5",
                    "client_5",
                    "client_9",
                    "client_9",
                    "client_9",
                    "client_9",
                ],
                "dialog_type": ["buy"] * 8,
                "is_sale": [False] * 8,
                "is_alarm_triggered": [False] * 8,
                "session_id": [f"s{i}" for i in range(8)],
            }
        ).to_excel(w, index=False, sheet_name="storeA")

        pd.DataFrame(
            {
                "created_at": [
                    "2026-08-27 11:00:00",
                    "2026-08-27 11:01:00",
                    "2026-08-27 11:02:00",
                    "2026-08-27 11:03:00",
                    "2026-08-27 11:04:00",
                ],
                "recognition_text": [
                    "здравствуйте",
                    "мне нужна",
                    "спасибо",
                    "здравствуйте",
                    "до свидания",
                ],
                "seller_id": ["petr"] * 5,
                "client_id": [
                    None,
                    "client_5",
                    "client_5",
                    "client_5",
                    "client_5",
                ],
                "dialog_type": ["buy"] * 5,
                "is_sale": [False] * 5,
                "is_alarm_triggered": [False] * 5,
                "session_id": [f"t{i}" for i in range(5)],
            }
        ).to_excel(w, index=False, sheet_name="storeB")

    out = tmp_path / "final.xlsx"
    module = importlib.import_module("stats_service.create_final_report")
    module.create_final_report(str(raw), str(out))

    result = pd.read_excel(out)
    per_store = {}
    for _, r in result.iterrows():
        per_store.setdefault(r["store_id"], []).append((r["client_id"], r["internal_client_id"]))
    assert per_store["storeA"] == [
        ("client_1", "client_5"),
        ("client_2", "client_9"),
    ]
    assert per_store["storeB"] == [
        ("client_1", "client_5"),
    ]


def test_create_final_report_preserves_internal_client_id(tmp_path):
    _stub_optional_deps()

    raw = tmp_path / "raw.xlsx"
    with pd.ExcelWriter(raw, engine="openpyxl") as w:
        pd.DataFrame(
            {
                "created_at": [
                    "2026-08-27 10:00:00",
                    "2026-08-27 10:01:00",
                    "2026-08-27 10:02:00",
                ],
                "recognition_text": ["здравствуйте", "хочу", "до свидания"],
                "seller_id": ["ivan"] * 3,
                "client_id": ["client_21", "client_21", "client_21"],
                "dialog_type": ["buy"] * 3,
                "is_sale": [False] * 3,
                "is_alarm_triggered": [False] * 3,
                "session_id": ["s0", "s1", "s2"],
            }
        ).to_excel(w, index=False, sheet_name="storeZ")

    out = tmp_path / "final.xlsx"
    module = importlib.import_module("stats_service.create_final_report")
    module.create_final_report(str(raw), str(out))

    result = pd.read_excel(out)
    assert list(result["client_id"]) == ["client_1"]
    assert list(result["internal_client_id"]) == ["client_21"]
