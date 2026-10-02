from fastapi.testclient import TestClient
import sys
from pathlib import Path
import os

# Set REPORT_DIR before importing api
os.environ["REPORT_OUTPUT_DIR"] = str(Path(__file__).parent.parent / "reports")

# Remove cached modules to force reimport with new env var
modules_to_remove = [k for k in sys.modules.keys() if k.startswith('stats_service')]
for mod in modules_to_remove:
    del sys.modules[mod]

from stats_service.api import app

client = TestClient(app)


def test_get_stores():
    """Test that GET /api/stores returns fixed list of stores"""
    response = client.get("/api/stores")
    assert response.status_code == 200
    data = response.json()
    assert "stores" in data
    assert isinstance(data["stores"], list)
    assert len(data["stores"]) > 0
    # Should contain known stores
    assert "г_Минеральные_Воды_ул_Гагарина_" in data["stores"]
    assert "г_Пятигорск_ул_Первомайская_д_3" in data["stores"]


def test_get_reports_dates():
    """Test that GET /api/reports/dates returns available dates"""
    response = client.get("/api/reports/dates")
    assert response.status_code == 200
    data = response.json()
    assert "dates" in data
    assert isinstance(data["dates"], list)
    # Should contain at least 2026-08-27
    assert "2026-08-27" in data["dates"]


def test_get_report_data_valid_date():
    """Test that GET /api/reports/{date}/data returns report data"""
    response = client.get("/api/reports/2026-08-27/data")
    assert response.status_code == 200, response.text
    data = response.json()
    assert "date" in data
    assert "dialogs" in data
    assert "stores" in data
    assert "clients" in data
    assert data["date"] == "2026-08-27"
    assert isinstance(data["dialogs"], list)
    assert len(data["dialogs"]) > 0


def test_get_report_data_invalid_date_format():
    """Test that invalid date format returns 400"""
    response = client.get("/api/reports/invalid-date/data")
    assert response.status_code == 400


def test_get_report_data_nonexistent_date():
    """Test that nonexistent date returns 404"""
    response = client.get("/api/reports/2099-12-31/data")
    assert response.status_code == 404


def test_get_report_data_with_store_filter():
    """Test filtering by store"""
    response = client.get(
        "/api/reports/2026-08-27/data?store_filter=г_Минеральные_Воды_ул_Гагарина_"
    )
    assert response.status_code == 200
    data = response.json()
    # All dialogs should be from the filtered store
    for dialog in data["dialogs"]:
        assert dialog["store_id"] == "г_Минеральные_Воды_ул_Гагарина_"


def test_get_report_data_with_client_filter():
    """Test filtering by client"""
    response = client.get("/api/reports/2026-08-27/data?client_filter=client_1")
    assert response.status_code == 200
    data = response.json()
    # All dialogs should be from the filtered client
    for dialog in data["dialogs"]:
        assert dialog["client_id"] == "client_1"


def test_get_report_data_dialog_structure():
    """Test that dialog data has correct structure"""
    response = client.get("/api/reports/2026-08-27/data")
    assert response.status_code == 200
    data = response.json()
    dialog = data["dialogs"][0]
    
    assert "client_id" in dialog
    assert "store_id" in dialog
    assert "seller_id" in dialog
    assert "start_time" in dialog
    assert "end_time" in dialog
    assert "duration_sec" in dialog
    assert "session_ids" in dialog
    assert "is_sale" in dialog
    assert "dialog_type" in dialog
    
    # session_ids should be a list
    assert isinstance(dialog["session_ids"], list)


def test_get_report_data_stores_and_clients():
    """Test that stores and clients are returned correctly"""
    response = client.get("/api/reports/2026-08-27/data")
    assert response.status_code == 200
    data = response.json()
    
    assert isinstance(data["stores"], list)
    assert isinstance(data["clients"], list)
    assert len(data["stores"]) > 0
    assert len(data["clients"]) > 0


def test_audio_page_returns_html():
    """Test that /audio page returns HTML"""
    response = client.get("/audio")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text.lower()


def test_audio_page_has_date_select():
    """Test that audio page has date select element"""
    response = client.get("/audio")
    assert response.status_code == 200
    assert 'id="date-select"' in response.text


def test_audio_page_has_store_select():
    """Test that audio page has store select element"""
    response = client.get("/audio")
    assert response.status_code == 200
    assert 'id="store-select"' in response.text


def test_audio_page_no_client_select():
    """Test that audio page doesn't have client select (removed from UI)"""
    response = client.get("/audio")
    assert response.status_code == 200
    assert 'id="client-select"' not in response.text


def test_audio_time_range_page_exists():
    """Test that /audio/time-range page exists"""
    response = client.get("/audio/time-range")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text.lower()
    assert "Скачать аудио по времени" in response.text
    assert 'id="store-select"' in response.text
    assert 'id="start_time"' in response.text
    assert 'id="end_time"' in response.text


def test_index_page_has_both_audio_links():
    """Test that index page has links to both audio pages"""
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/audio"' in response.text
    assert 'href="/audio/time-range"' in response.text
