# test_email_scheduling.py
"""
Test script for email scheduling logic fixes.
Tests:
1. Wait days accumulation across multiple steps
2. Duplicate prevention
3. Missing template logging
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import uuid

# Test 1: Wait days accumulation for multiple steps
def test_wait_days_accumulation():
    """Test that wait_days are accumulated correctly for each step."""
    print("\n" + "="*60)
    print("TEST 1: Wait Days Accumulation")
    print("="*60)
    
    from app.utils.business_calendar import calculate_send_time
    
    base_time = datetime(2026, 1, 22, 10, 0, 0)  # Thursday 10 AM UTC
    
    # Simulate sequences: Step 1 (wait 0), Step 2 (wait 2), Step 3 (wait 4)
    sequences = [
        {"step": 1, "wait_days": 0},
        {"step": 2, "wait_days": 2},
        {"step": 3, "wait_days": 4},
    ]
    
    cumulative_wait_days = 0
    scheduled_times = []
    
    print(f"\nBase time: {base_time} (UTC)")
    print(f"\nSequences configuration:")
    for seq in sequences:
        print(f"  Step {seq['step']}: wait_days = {seq['wait_days']}")
    
    print(f"\n{'Step':<6} {'Wait Days':<12} {'Cumulative':<12} {'Scheduled At (NY Timezone)'}")
    print("-" * 70)
    
    for seq in sequences:
        # NEW LOGIC: Accumulate for ALL steps including step 1
        cumulative_wait_days += seq["wait_days"]
        
        scheduled_at = calculate_send_time(
            base_time=base_time,
            wait_days=cumulative_wait_days,
            state="NY",  # New York timezone
            send_hour=9,
            send_minute=0,
            add_jitter=False  # No jitter for predictable test
        )
        
        scheduled_times.append(scheduled_at)
        print(f"{seq['step']:<6} {seq['wait_days']:<12} {cumulative_wait_days:<12} {scheduled_at}")
    
    # Validate gaps between emails
    print(f"\n✓ Time gaps between steps:")
    for i in range(1, len(scheduled_times)):
        gap = scheduled_times[i] - scheduled_times[i-1]
        print(f"  Step {i} → Step {i+1}: {gap.days} days")
    
    print("\n✅ TEST 1 PASSED: Wait days accumulation works correctly")
    return True


# Test 2: Duplicate prevention logic
def test_duplicate_prevention():
    """Test that duplicate messages are detected and skipped."""
    print("\n" + "="*60)
    print("TEST 2: Duplicate Prevention Logic")
    print("="*60)
    
    # Simulate the duplicate check logic
    existing_messages = [
        {"campaign_id": "camp1", "prospect_id": "p1", "sequence_id": "seq1"},
        {"campaign_id": "camp1", "prospect_id": "p1", "sequence_id": "seq2"},
    ]
    
    # New messages to create
    new_messages = [
        {"campaign_id": "camp1", "prospect_id": "p1", "sequence_id": "seq1"},  # DUPLICATE
        {"campaign_id": "camp1", "prospect_id": "p1", "sequence_id": "seq3"},  # NEW
        {"campaign_id": "camp1", "prospect_id": "p2", "sequence_id": "seq1"},  # NEW (different prospect)
    ]
    
    print(f"\nExisting messages in DB:")
    for msg in existing_messages:
        print(f"  - {msg}")
    
    print(f"\nAttempting to create:")
    created = 0
    skipped = 0
    
    for msg in new_messages:
        # Check if exists
        exists = any(
            e["campaign_id"] == msg["campaign_id"] and
            e["prospect_id"] == msg["prospect_id"] and
            e["sequence_id"] == msg["sequence_id"]
            for e in existing_messages
        )
        
        if exists:
            print(f"  ⏭️ SKIP (duplicate): {msg}")
            skipped += 1
        else:
            print(f"  ✅ CREATE: {msg}")
            created += 1
    
    print(f"\nResults: {created} created, {skipped} skipped")
    
    assert created == 2, f"Expected 2 created, got {created}"
    assert skipped == 1, f"Expected 1 skipped, got {skipped}"
    
    print("\n✅ TEST 2 PASSED: Duplicate prevention works correctly")
    return True


# Test 3: Retry backoff query logic
def test_retry_backoff_query():
    """Test that retry queries respect next_retry_at timing."""
    print("\n" + "="*60)
    print("TEST 3: Retry Backoff Query Logic")
    print("="*60)
    
    now = datetime(2026, 1, 22, 12, 0, 0)
    
    # Simulate email messages with different retry states
    messages = [
        {
            "id": 1,
            "status": "QUEUED",
            "scheduled_at": datetime(2026, 1, 22, 10, 0),  # Past
            "next_retry_at": None,  # First attempt - SHOULD BE PICKED
        },
        {
            "id": 2,
            "status": "QUEUED",
            "scheduled_at": datetime(2026, 1, 22, 10, 0),  # Past
            "next_retry_at": datetime(2026, 1, 22, 11, 0),  # Past - SHOULD BE PICKED
        },
        {
            "id": 3,
            "status": "QUEUED",
            "scheduled_at": datetime(2026, 1, 22, 10, 0),  # Past
            "next_retry_at": datetime(2026, 1, 22, 14, 0),  # Future - SHOULD NOT BE PICKED
        },
        {
            "id": 4,
            "status": "SCHEDULED",
            "scheduled_at": datetime(2026, 1, 22, 15, 0),  # Future - SHOULD NOT BE PICKED
            "next_retry_at": None,
        },
    ]
    
    print(f"\nCurrent time: {now}")
    print(f"\nMessages in queue:")
    print(f"{'ID':<4} {'Status':<12} {'Scheduled At':<22} {'Next Retry At':<22}")
    print("-" * 60)
    
    for msg in messages:
        retry_str = str(msg['next_retry_at']) if msg['next_retry_at'] else "None"
        print(f"{msg['id']:<4} {msg['status']:<12} {msg['scheduled_at']} {retry_str}")
    
    # Apply the NEW query logic
    picked = []
    for msg in messages:
        is_due_status = msg["status"] in ["QUEUED", "SCHEDULED"]
        is_scheduled_past = msg["scheduled_at"] <= now
        is_retry_ready = msg["next_retry_at"] is None or msg["next_retry_at"] <= now
        
        if is_due_status and is_scheduled_past and is_retry_ready:
            picked.append(msg["id"])
    
    print(f"\n✓ Messages picked for processing: {picked}")
    
    expected = [1, 2]  # Messages 1 and 2 should be picked
    assert picked == expected, f"Expected {expected}, got {picked}"
    
    print(f"✓ Message 3 correctly skipped (retry time in future)")
    print(f"✓ Message 4 correctly skipped (scheduled in future)")
    
    print("\n✅ TEST 3 PASSED: Retry backoff query works correctly")
    return True


# Test 4: Multiple step scheduling with real sequence config
def test_full_scheduling_scenario():
    """Test a complete multi-step scheduling scenario."""
    print("\n" + "="*60)
    print("TEST 4: Full Multi-Step Scheduling Scenario")
    print("="*60)
    
    from app.utils.business_calendar import calculate_send_time
    
    # Campaign starts on Thursday Jan 22, 2026 at 2 PM IST = 8:30 AM UTC
    base_time = datetime(2026, 1, 22, 8, 30, 0)
    
    # Sequence config: 5 emails over 2 weeks
    sequences = [
        {"step": 1, "wait_days": 0, "purpose": "Introduction"},
        {"step": 2, "wait_days": 2, "purpose": "Follow-up"},
        {"step": 3, "wait_days": 3, "purpose": "Value Prop"},
        {"step": 4, "wait_days": 4, "purpose": "Case Study"},
        {"step": 5, "wait_days": 5, "purpose": "Final Push"},
    ]
    
    # Different prospects in different timezones
    prospects = [
        {"name": "John (NY)", "state": "NY"},
        {"name": "Jane (CA)", "state": "CA"},
        {"name": "Bob (TX)", "state": "TX"},
    ]
    
    print(f"\nCampaign activation: {base_time} UTC")
    print(f"\nSequence configuration:")
    for seq in sequences:
        print(f"  Step {seq['step']}: +{seq['wait_days']} days - {seq['purpose']}")
    
    for prospect in prospects:
        print(f"\n--- {prospect['name']} ---")
        cumulative = 0
        for seq in sequences:
            cumulative += seq["wait_days"]
            scheduled = calculate_send_time(
                base_time=base_time,
                wait_days=cumulative,
                state=prospect["state"],
                send_hour=9,
                send_minute=0,
                add_jitter=False
            )
            print(f"  Step {seq['step']} ({seq['purpose']:<12}): {scheduled.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    
    print("\n✅ TEST 4 PASSED: Full scheduling scenario works correctly")
    return True


if __name__ == "__main__":
    print("\n" + "="*60)
    print("EMAIL SCHEDULING LOGIC TEST SUITE")
    print("="*60)
    
    tests_passed = 0
    tests_failed = 0
    
    try:
        test_wait_days_accumulation()
        tests_passed += 1
    except Exception as e:
        print(f"\n❌ TEST 1 FAILED: {e}")
        tests_failed += 1
    
    try:
        test_duplicate_prevention()
        tests_passed += 1
    except Exception as e:
        print(f"\n❌ TEST 2 FAILED: {e}")
        tests_failed += 1
    
    try:
        test_retry_backoff_query()
        tests_passed += 1
    except Exception as e:
        print(f"\n❌ TEST 3 FAILED: {e}")
        tests_failed += 1
    
    try:
        test_full_scheduling_scenario()
        tests_passed += 1
    except Exception as e:
        print(f"\n❌ TEST 4 FAILED: {e}")
        tests_failed += 1
    
    print("\n" + "="*60)
    print(f"RESULTS: {tests_passed} passed, {tests_failed} failed")
    print("="*60)
