# Test script for business_calendar.py
# Run from backend directory: python -m app.utils.test_business_calendar

from datetime import datetime, date
from app.utils.business_calendar import (
    normalize_state_code,
    get_timezone_for_state,
    is_us_holiday,
    is_weekend,
    is_business_day,
    get_next_business_day,
    calculate_send_time,
    get_holiday_name,
)

def test_state_normalization():
    """Test state code normalization"""
    print("\n=== Testing State Normalization ===")
    test_cases = [
        ("CA", "CA"),
        ("California", "CA"),
        ("california", "CA"),
        ("CALIFORNIA", "CA"),
        ("Calif", "CA"),
        ("NY", "NY"),
        ("New York", "NY"),
        ("new york", "NY"),
        ("TX", "TX"),
        ("Texas", "TX"),
        ("texas", "TX"),
        ("FL", "FL"),
        ("Florida", "FL"),
        ("WA", "WA"),
        ("Washington", "WA"),
        ("Invalid", None),
    ]
    
    for input_val, expected in test_cases:
        result = normalize_state_code(input_val)
        status = "✓" if result == expected else "✗"
        print(f"  {status} normalize_state_code('{input_val}') = {result} (expected: {expected})")


def test_timezone_mapping():
    """Test timezone resolution from state"""
    print("\n=== Testing Timezone Mapping ===")
    test_cases = [
        ("CA", "America/Los_Angeles"),
        ("NY", "America/New_York"),
        ("TX", "America/Chicago"),
        ("FL", "America/New_York"),
        ("AZ", "America/Phoenix"),
        ("CO", "America/Denver"),
        ("HI", "Pacific/Honolulu"),
        ("AK", "America/Anchorage"),
        ("California", "America/Los_Angeles"),
        ("New York", "America/New_York"),
        (None, "America/New_York"),  # Default
    ]
    
    for state, expected_tz in test_cases:
        result = get_timezone_for_state(state=state)
        status = "✓" if result == expected_tz else "✗"
        print(f"  {status} get_timezone_for_state('{state}') = {result} (expected: {expected_tz})")


def test_holiday_detection():
    """Test US holiday detection"""
    print("\n=== Testing Holiday Detection ===")
    # 2026 US Federal Holidays
    holidays_2026 = [
        (datetime(2026, 1, 1), "New Year's Day"),
        (datetime(2026, 1, 19), "Martin Luther King Jr. Day"),
        (datetime(2026, 2, 16), "Washington's Birthday"),
        (datetime(2026, 5, 25), "Memorial Day"),
        (datetime(2026, 7, 3), "Independence Day (Observed)"),  # July 4 is Saturday
        (datetime(2026, 9, 7), "Labor Day"),
        (datetime(2026, 11, 11), "Veterans Day"),
        (datetime(2026, 11, 26), "Thanksgiving"),
        (datetime(2026, 12, 25), "Christmas Day"),
    ]
    
    for dt, name in holidays_2026:
        is_holiday = is_us_holiday(dt)
        holiday_name = get_holiday_name(dt)
        status = "✓" if is_holiday else "✗"
        print(f"  {status} {dt.strftime('%Y-%m-%d')}: is_us_holiday={is_holiday}, name='{holiday_name}' (expected: {name})")
    
    # Non-holidays
    print("\n  -- Non-holidays --")
    non_holidays = [
        datetime(2026, 1, 2),
        datetime(2026, 3, 15),
        datetime(2026, 6, 10),
    ]
    for dt in non_holidays:
        is_holiday = is_us_holiday(dt)
        status = "✓" if not is_holiday else "✗"
        print(f"  {status} {dt.strftime('%Y-%m-%d')}: is_us_holiday={is_holiday} (expected: False)")


def test_weekend_detection():
    """Test weekend detection"""
    print("\n=== Testing Weekend Detection ===")
    test_cases = [
        (datetime(2026, 1, 15), False, "Thursday"),
        (datetime(2026, 1, 16), False, "Friday"),
        (datetime(2026, 1, 17), True, "Saturday"),
        (datetime(2026, 1, 18), True, "Sunday"),
        (datetime(2026, 1, 19), False, "Monday"),
    ]
    
    for dt, expected_weekend, day_name in test_cases:
        result = is_weekend(dt)
        status = "✓" if result == expected_weekend else "✗"
        print(f"  {status} {dt.strftime('%Y-%m-%d')} ({day_name}): is_weekend={result} (expected: {expected_weekend})")


def test_business_day_calculation():
    """Test next business day calculation"""
    print("\n=== Testing Next Business Day ===")
    
    # Test 1: Saturday -> Monday
    sat = datetime(2026, 1, 17)  # Saturday
    next_biz, reason = get_next_business_day(sat)
    expected = datetime(2026, 1, 19)  # Monday
    status = "✓" if next_biz.date() == expected.date() else "✗"
    print(f"  {status} Saturday 2026-01-17 -> {next_biz.strftime('%Y-%m-%d')} ({next_biz.strftime('%A')}) [reason: {reason}]")
    
    # Test 2: Sunday -> Monday
    sun = datetime(2026, 1, 18)  # Sunday
    next_biz, reason = get_next_business_day(sun)
    expected = datetime(2026, 1, 19)  # Monday
    status = "✓" if next_biz.date() == expected.date() else "✗"
    print(f"  {status} Sunday 2026-01-18 -> {next_biz.strftime('%Y-%m-%d')} ({next_biz.strftime('%A')}) [reason: {reason}]")
    
    # Test 3: MLK Day (Monday holiday) -> Tuesday
    mlk_day = datetime(2026, 1, 19)  # MLK Day (Monday)
    next_biz, reason = get_next_business_day(mlk_day)
    expected = datetime(2026, 1, 20)  # Tuesday
    status = "✓" if next_biz.date() == expected.date() else "✗"
    print(f"  {status} MLK Day 2026-01-19 -> {next_biz.strftime('%Y-%m-%d')} ({next_biz.strftime('%A')}) [reason: {reason}]")
    
    # Test 4: Regular weekday -> same day
    wed = datetime(2026, 1, 21)  # Wednesday
    next_biz, reason = get_next_business_day(wed)
    status = "✓" if next_biz.date() == wed.date() else "✗"
    print(f"  {status} Wednesday 2026-01-21 -> {next_biz.strftime('%Y-%m-%d')} (no skip expected) [reason: {reason}]")


def test_calculate_send_time():
    """Test full send time calculation"""
    print("\n=== Testing calculate_send_time ===")
    
    base_time = datetime(2026, 1, 15, 10, 0, 0)  # Thursday 10:00 UTC
    
    # Test 1: California prospect, 0 wait days (should be 9 AM PST)
    result = calculate_send_time(base_time, wait_days=0, state="CA", add_jitter=False)
    print(f"  CA, 0 days: {result.strftime('%Y-%m-%d %H:%M')} UTC (should be ~17:00 UTC = 9 AM PST)")
    
    # Test 2: New York prospect, 0 wait days (should be 9 AM EST)
    result = calculate_send_time(base_time, wait_days=0, state="NY", add_jitter=False)
    print(f"  NY, 0 days: {result.strftime('%Y-%m-%d %H:%M')} UTC (should be ~14:00 UTC = 9 AM EST)")
    
    # Test 3: TX prospect, 2 wait days from Thursday -> Monday (skip weekend)
    result = calculate_send_time(base_time, wait_days=2, state="TX", add_jitter=False)
    print(f"  TX, 2 days: {result.strftime('%Y-%m-%d %H:%M')} UTC (Thu+2=Sat -> Mon)")
    
    # Test 4: NY prospect, 4 days from Thursday -> Wed (skip Sat+Sun+MLK Monday)
    result = calculate_send_time(base_time, wait_days=4, state="NY", add_jitter=False)
    print(f"  NY, 4 days: {result.strftime('%Y-%m-%d %H:%M')} UTC (Thu+4=Mon MLK -> Tue, actually Wed due to holidays)")


def test_is_within_send_window():
    """Test send window validation"""
    print("\n=== Testing is_within_send_window ===")
    
    from app.utils.business_calendar import is_within_send_window
    from datetime import time
    
    # Send window: 09:00 to 17:00
    start = time(9, 0)
    end = time(17, 0)
    
    # Setup test cases: (Current UTC Time, Timezone, Expected Result, Description)
    test_cases = [
        # 1. New York (EST/EDT)
        # EST is UTC-5. 14:00 UTC = 09:00 EST (Valid)
        (datetime(2026, 1, 15, 14, 0, 0), "America/New_York", True, "NY: exactly at start time"),
        # EST is UTC-5. 19:00 UTC = 14:00 EST (Valid)
        (datetime(2026, 1, 15, 19, 0, 0), "America/New_York", True, "NY: middle of day"),
        # EST is UTC-5. 23:00 UTC = 18:00 EST (Invalid - too late)
        (datetime(2026, 1, 15, 23, 0, 0), "America/New_York", False, "NY: after end time"),
        # EST is UTC-5. 12:00 UTC = 07:00 EST (Invalid - too early)
        (datetime(2026, 1, 15, 12, 0, 0), "America/New_York", False, "NY: before start time"),
        
        # 2. Los Angeles (PST/PDT)
        # PST is UTC-8. 17:00 UTC = 09:00 PST (Valid)
        (datetime(2026, 1, 15, 17, 0, 0), "America/Los_Angeles", True, "LA: exactly at start time"),
        # PST is UTC-8. 14:00 UTC = 06:00 PST (Invalid - too early)
        (datetime(2026, 1, 15, 14, 0, 0), "America/Los_Angeles", False, "LA: before start time"),
        
        # 3. Weekend/Holiday handling (NY)
        # 2026-01-17 is a Saturday
        (datetime(2026, 1, 17, 15, 0, 0), "America/New_York", False, "NY: valid time but on Saturday"),
        # 2026-01-19 is MLK Day
        (datetime(2026, 1, 19, 15, 0, 0), "America/New_York", False, "NY: valid time but on US holiday"),
        
        # 4. UTC implicitly handled
        (datetime(2026, 1, 15, 10, 0, 0), "UTC", True, "UTC: middle of day"),
        (datetime(2026, 1, 15, 8, 0, 0), "UTC", False, "UTC: before start time"),
    ]
    
    for utc_now, tz_name, expected, desc in test_cases:
        result = is_within_send_window(
            current_utc=utc_now,
            timezone_str=tz_name,
            send_window_start=start,
            send_window_end=end
        )
        status = "✓" if result == expected else "✗"
        print(f"  {status} {desc}")
        print(f"      Input: {utc_now.strftime('%Y-%m-%d %H:%M:%S')} UTC, '{tz_name}' -> {result} (Expected {expected})")


if __name__ == "__main__":
    print("=" * 60)
    print("Business Calendar Test Suite")
    print("Current date:", datetime.now().strftime('%Y-%m-%d %H:%M'))
    print("=" * 60)
    
    test_state_normalization()
    test_timezone_mapping()
    test_holiday_detection()
    test_weekend_detection()
    test_business_day_calculation()
    test_calculate_send_time()
    test_is_within_send_window()
    
    print("\n" + "=" * 60)
    print("Tests completed!")
    print("=" * 60)
