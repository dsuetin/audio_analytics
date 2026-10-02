"""Tests for manual dialogs functionality"""
import pytest
from fastapi.testclient import TestClient
from stats_service.api import app, MANUAL_DIALOGS_FILE, REPORT_DIR
import json
import os
import tempfile
from pathlib import Path

# Create temp directory for tests
TEST_REPORT_DIR = Path(tempfile.mkdtemp(prefix="audio_analytics_test_"))
TEST_MANUAL_DIALOGS_FILE = TEST_REPORT_DIR / "manual_dialogs.json"

# Monkey patch for tests
import stats_service.api
stats_service.api.MANUAL_DIALOGS_FILE = TEST_MANUAL_DIALOGS_FILE
stats_service.api.REPORT_DIR = TEST_REPORT_DIR

client = TestClient(app)


def cleanup_manual_dialogs():
    """Remove manual dialogs file if exists"""
    if TEST_MANUAL_DIALOGS_FILE.exists():
        TEST_MANUAL_DIALOGS_FILE.unlink()


@pytest.fixture(autouse=True)
def setup_teardown():
    """Setup and teardown for each test"""
    TEST_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_manual_dialogs()
    yield
    cleanup_manual_dialogs()
    # Cleanup temp dir
    import shutil
    if TEST_REPORT_DIR.exists():
        shutil.rmtree(TEST_REPORT_DIR, ignore_errors=True)


class TestManualDialogsAPI:
    """Tests for manual dialogs API endpoints"""
    
    def test_get_empty_manual_dialogs(self):
        """Test getting manual dialogs when none exist"""
        response = client.get("/api/manual-dialogs")
        assert response.status_code == 200
        data = response.json()
        assert "dialogs" in data
        assert data["dialogs"] == []
    
    def test_create_manual_dialog_success(self):
        """Test creating a valid manual dialog"""
        dialog_data = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            "dialog_type": "Покупка товара",
            "result": "Клиент совершил покупку"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog_data)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "dialog" in data
        assert data["dialog"]["date"] == "2026-08-27"
        assert data["dialog"]["store_id"] == "Test Store"
        assert data["dialog"]["start_time"] == "10:25:00"
        assert data["dialog"]["end_time"] == "10:30:00"
        assert data["dialog"]["is_manual"] == True
        assert "id" in data["dialog"]
    
    def test_create_manual_dialog_missing_field(self):
        """Test creating manual dialog with missing required field"""
        dialog_data = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            # Missing dialog_type
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog_data)
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "Обязательное поле отсутствует" in data["detail"]
    
    def test_create_manual_dialog_start_after_end(self):
        """Test creating manual dialog with start_time >= end_time"""
        dialog_data = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:30:00",
            "end_time": "10:25:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog_data)
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "Время начала должно быть раньше" in data["detail"]
    
    def test_create_manual_dialog_overlap_previous(self):
        """Test creating manual dialog that overlaps with previous session"""
        # Create first dialog
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:20:00",
            "end_time": "10:25:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        # Try to create overlapping dialog (starts before first ends)
        dialog2 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:23:00",
            "end_time": "10:28:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog2)
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "пересекается" in data["detail"]
        assert "10:20:00" in data["detail"]
        assert "10:25:00" in data["detail"]
    
    def test_create_manual_dialog_overlap_next(self):
        """Test creating manual dialog that overlaps with next session"""
        # Create first dialog
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:30:00",
            "end_time": "10:35:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        # Try to create overlapping dialog (ends after second starts)
        dialog2 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:25:00",
            "end_time": "10:32:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog2)
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "пересекается" in data["detail"]
    
    def test_create_manual_dialog_valid_between_sessions(self):
        """Test creating manual dialog between two existing sessions"""
        # Create first dialog
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:20:00",
            "end_time": "10:25:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        # Create second dialog
        dialog2 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:30:00",
            "end_time": "10:35:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog2)
        
        # Create valid dialog between them
        dialog3 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:26:00",
            "end_time": "10:29:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog3)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
    
    def test_create_manual_dialog_different_store_no_overlap_check(self):
        """Test that dialogs in different stores don't overlap"""
        # Create dialog in store 1
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Store 1",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        # Create overlapping dialog in store 2 (should succeed)
        dialog2 = {
            "date": "2026-08-27",
            "store_id": "Store 2",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog2)
        assert response.status_code == 200
    
    def test_create_manual_dialog_different_date_no_overlap_check(self):
        """Test that dialogs on different dates don't overlap"""
        # Create dialog on date 1
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        # Create overlapping dialog on date 2 (should succeed)
        dialog2 = {
            "date": "2026-08-28",
            "store_id": "Test Store",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog2)
        assert response.status_code == 200
    
    def test_delete_manual_dialog(self):
        """Test deleting a manual dialog"""
        # Create dialog
        dialog_data = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        create_response = client.post("/api/manual-dialogs", json=dialog_data)
        dialog_id = create_response.json()["dialog"]["id"]
        
        # Delete dialog
        response = client.delete(f"/api/manual-dialogs/{dialog_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        
        # Verify deleted
        get_response = client.get("/api/manual-dialogs")
        assert len(get_response.json()["dialogs"]) == 0
    
    def test_get_manual_dialogs_with_filters(self):
        """Test getting manual dialogs with date and store filters"""
        # Create dialogs
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Store A",
            "start_time": "10:25:00",
            "end_time": "10:30:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        dialog2 = {
            "date": "2026-08-28",
            "store_id": "Store B",
            "start_time": "11:00:00",
            "end_time": "11:05:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog2)
        
        # Filter by date
        response = client.get("/api/manual-dialogs?date=2026-08-27")
        assert len(response.json()["dialogs"]) == 1
        assert response.json()["dialogs"][0]["store_id"] == "Store A"
        
        # Filter by store
        response = client.get("/api/manual-dialogs?store=Store B")
        assert len(response.json()["dialogs"]) == 1
        assert response.json()["dialogs"][0]["date"] == "2026-08-28"
    
    def test_create_manual_dialog_before_first(self):
        """Test creating manual dialog before first existing session"""
        # Create first dialog
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:30:00",
            "end_time": "10:35:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        # Create dialog before first (should succeed)
        dialog2 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:15:00",
            "end_time": "10:25:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog2)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
    
    def test_create_manual_dialog_overlap_with_first(self):
        """Test creating manual dialog that overlaps with first session"""
        # Create first dialog
        dialog1 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:30:00",
            "end_time": "10:35:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        client.post("/api/manual-dialogs", json=dialog1)
        
        # Try to create dialog that overlaps with first (should fail)
        dialog2 = {
            "date": "2026-08-27",
            "store_id": "Test Store",
            "start_time": "10:25:00",
            "end_time": "10:32:00",
            "dialog_type": "Покупка",
            "result": "Покупка"
        }
        
        response = client.post("/api/manual-dialogs", json=dialog2)
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "пересекается" in data["detail"]


# Integration tests skipped - require final_report file
# class TestManualDialogsIntegration:
#     """Integration tests for manual dialogs with report data"""
#     
#     def test_manual_dialog_appears_in_report_data(self):
#         """Test that manual dialog appears in /api/reports/{date}/data"""
#         # Create manual dialog
#         dialog_data = {
#             "date": "2026-08-27",
#             "store_id": "Test Store",
#             "start_time": "10:25:00",
#             "end_time": "10:30:00",
#             "dialog_type": "Покупка товара",
#             "result": "Клиент совершил покупку"
#         }
#         client.post("/api/manual-dialogs", json=dialog_data)
#         
#         # Get report data
#         response = client.get("/api/reports/2026-08-27/data")
#         assert response.status_code == 200
#         data = response.json()
#         
#         # Find manual dialog
#         manual_dialogs = [d for d in data["dialogs"] if d.get("is_manual")]
#         assert len(manual_dialogs) == 1
#         assert manual_dialogs[0]["start_time"] == "10:25:00"
#         assert manual_dialogs[0]["end_time"] == "10:30:00"
#         assert manual_dialogs[0]["dialog_type"] == "Покупка товара"
#         assert manual_dialogs[0]["result"] == "Клиент совершил покупку"
#     
#     def test_manual_dialogs_sorted_by_time(self):
#         """Test that manual dialogs are sorted by start_time in report data"""
#         # Create multiple manual dialogs
#         dialogs = [
#             {
#                 "date": "2026-08-27",
#                 "store_id": "Test Store",
#                 "start_time": "10:30:00",
#                 "end_time": "10:35:00",
#                 "dialog_type": "Покупка",
#                 "result": "Покупка"
#             },
#             {
#                 "date": "2026-08-27",
#                 "store_id": "Test Store",
#                 "start_time": "10:20:00",
#                 "end_time": "10:25:00",
#                 "dialog_type": "Покупка",
#                 "result": "Покупка"
#             },
#             {
#                 "date": "2026-08-27",
#                 "store_id": "Test Store",
#                 "start_time": "10:25:00",
#                 "end_time": "10:30:00",
#                 "dialog_type": "Покупка",
#                 "result": "Покупка"
#             }
#         ]
#         
#         for dialog in dialogs:
#             client.post("/api/manual-dialogs", json=dialog)
#         
#         # Get report data
#         response = client.get("/api/reports/2026-08-27/data")
#         data = response.json()
#         
#         # Check sorting
#         manual_dialogs = [d for d in data["dialogs"] if d.get("is_manual")]
#         times = [d["start_time"] for d in manual_dialogs]
#         assert times == sorted(times)
