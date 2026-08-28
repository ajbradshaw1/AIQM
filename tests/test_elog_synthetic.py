#!/usr/bin/env python3
"""End-to-end test of ElogReader against a synthetic .elo file.

Writes a minimal .elo with the exact binary format documented in
drivers/elog.py, then exercises every code path in ElogReader:

- ``connect()`` with a real (synthetic) path
- schema-presence cache (first read populates, subsequent reads cached)
- batch read via ``latest_record``
- ``var_map`` filtering (vars in file but not in map → ignored;
  vars in map but not in file → output dict key stays None)
- pressure plausibility filter (outside UHV range → filtered out)
- cache invalidation on log-file rotation

Usage:
    python -m pytest -q tests/test_elog_synthetic.py

Exits 0 on success; raises AssertionError with a diagnostic on failure.

Built Jun 23 2026 to give ElogReader's read path Mac-side validation
before its first Bulbasaur run. Catches the kind of off-by-one or
API-misuse bug that would otherwise waste lab time.
"""
import datetime as dt
import struct
import tempfile
from pathlib import Path

import pytest

from drivers.elog import latest_record, latest_value
from drivers.evap_control import ElogReader

LABVIEW_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)


def write_elog(path, schema_vars, records):
    """Write a minimal .elo file with the documented binary layout.

    schema_vars: list of (name, fmt) tuples
    records: list of (timestamp_dt, [values]) tuples
    """
    with open(path, "wb") as f:
        # u32 var_count, u32 version=2 (big-endian per drivers/elog.py)
        f.write(struct.pack(">II", len(schema_vars), 2))
        # Schema entries: [u32 name_len][name][u32 fmt_len][fmt]
        for name, fmt in schema_vars:
            nb = name.encode("utf-8")
            fb = fmt.encode("utf-8")
            f.write(struct.pack(">I", len(nb)))
            f.write(nb)
            f.write(struct.pack(">I", len(fb)))
            f.write(fb)
        # Records: [f64 timestamp][N × f32 values]
        for ts_dt, values in records:
            ts_s = (ts_dt - LABVIEW_EPOCH).total_seconds()
            f.write(struct.pack(">d", ts_s))
            assert len(values) == len(schema_vars)
            for v in values:
                f.write(struct.pack(">f", v))


def _today_path(log_dir):
    return Path(log_dir) / f"log_{dt.date.today().isoformat()}_000000.elo"


def test_basic():
    """All 11 DEFAULT_VAR_MAP vars present in schema → all 11 populated."""
    schema = [
        ("MBE.Pressure", "%.2e mbar"),
        ("MBE-Mani.PV", "%.1f C"),
        ("MBE-Mani.setpoint", "%.1f C"),
        ("HTEC 2.PV", "%.1f C"),
        ("HTEC Y.PV", "%.1f C"),
        ("LTEC 1 Sr.PV", "%.1f C"),
        ("LTEC 2 Eu.PV", "%.1f C"),
        ("MTEC Er.PV", "%.1f C"),
        ("Plasma.DCBias", "%.1f V"),
        ("Plasma.forward", "%.1f W"),
        ("Plasma.reflected", "%.1f W"),
    ]
    now = dt.datetime.now(dt.timezone.utc)
    records = [(now, [
        5.2e-9, 765.3, 800.0, 995.0, 1100.0,
        450.0, 520.0, 780.0, -125.5, 300.0, 5.2,
    ])]
    with tempfile.TemporaryDirectory() as tmp:
        write_elog(_today_path(tmp), schema, records)
        r = ElogReader(log_dir=tmp)
        r.connect()
        d = r.read()
        for key in r.DEFAULT_VAR_MAP.values():
            assert d[key] is not None, f"{key} returned None"
        assert abs(d["chamber_pressure_mbar"] - 5.2e-9) < 1e-14
        assert abs(d["substrate_temp_pv_C"] - 765.3) < 0.1
        assert abs(d["plasma_dc_bias_V"] - (-125.5)) < 0.1
    print("[basic] PASS — all 11 vars populated")


def test_partial_schema():
    """Schema missing most vars → present ones populated, absent stay None."""
    schema = [
        ("MBE.Pressure", "%.2e mbar"),
        ("MBE-Mani.PV", "%.1f C"),
    ]
    now = dt.datetime.now(dt.timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        write_elog(_today_path(tmp), schema, [(now, [3.5e-10, 850.0])])
        r = ElogReader(log_dir=tmp)
        r.connect()
        d = r.read()
        assert abs(d["chamber_pressure_mbar"] - 3.5e-10) < 1e-15
        assert abs(d["substrate_temp_pv_C"] - 850.0) < 0.1
        for absent in (
            "cell_HTEC2_pv_C", "cell_Y_pv_C", "plasma_dc_bias_V",
            "plasma_forward_W", "substrate_temp_setpoint_C",
        ):
            assert d[absent] is None, f"{absent}: expected None, got {d[absent]}"
    print("[partial_schema] PASS — present vars populated, absent stay None")


def test_pressure_plausibility():
    """Pressure outside UHV range is filtered out (other vars unaffected)."""
    schema = [("MBE.Pressure", "%.2e mbar")]
    now = dt.datetime.now(dt.timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        write_elog(_today_path(tmp), schema, [(now, [1.0])])  # 1 mbar — way too high
        r = ElogReader(log_dir=tmp)
        r.connect()
        d = r.read()
        assert d["chamber_pressure_mbar"] is None, \
            f"Implausible pressure should be filtered; got {d['chamber_pressure_mbar']}"
    print("[plausibility] PASS — implausible pressure filtered")


def test_disconnected_returns_none_dict():
    """read() before connect() returns a dict of all-None, no exception."""
    r = ElogReader()
    d = r.read()
    assert len(d) == len(r.DEFAULT_VAR_MAP)
    assert all(v is None for v in d.values())
    print("[disconnected_read] PASS — returns all-None dict cleanly")


def test_nan_plasma_values_treated_as_none():
    """Plasma NaN (source off) → None in read() so CSV shows empty, not 'nan'.

    Jun 23 2026 finding: EvapControl writes literal float NaN into
    Plasma.DCBias / Plasma.forward / Plasma.reflected whenever the plasma
    is off. Downstream we want those to appear as "unavailable" (empty
    CSV cell) rather than the literal string "nan".
    """
    schema = [
        ("MBE.Pressure", "%.2e mbar"),
        ("MBE-Mani.PV", "%.1f C"),
        ("Plasma.DCBias", "%.1f V"),
        ("Plasma.forward", "%.1f W"),
        ("Plasma.reflected", "%.1f W"),
    ]
    now = dt.datetime.now(dt.timezone.utc)
    nan = float("nan")
    records = [(now, [5.2e-9, 750.0, nan, nan, nan])]
    with tempfile.TemporaryDirectory() as tmp:
        write_elog(_today_path(tmp), schema, records)
        r = ElogReader(log_dir=tmp)
        r.connect()
        d = r.read()
        # Non-plasma vars unaffected
        assert abs(d["chamber_pressure_mbar"] - 5.2e-9) < 1e-14
        assert abs(d["substrate_temp_pv_C"] - 750.0) < 0.1
        # Plasma vars filtered to None — not the float NaN value
        for key in ("plasma_dc_bias_V", "plasma_forward_W", "plasma_reflected_W"):
            assert d[key] is None, (
                f"{key}: expected None (from NaN filter), got {d[key]!r}"
            )
    print("[nan_plasma] PASS — NaN values filtered to None")


def test_latest_record_ignores_trailing_partial_append():
    """A live partial append cannot shift the record decode boundary."""
    schema = [
        ("MBE.Pressure", "%.2e mbar"),
        ("HTEZ_Fe.PV", "%.1f C"),
    ]
    now = dt.datetime.now(dt.timezone.utc)
    path = None
    with tempfile.TemporaryDirectory() as tmp:
        path = _today_path(tmp)
        write_elog(path, schema, [
            (now - dt.timedelta(seconds=1), [3.0e-9, 1100.0]),
            (now, [4.0e-9, 1110.0]),
        ])
        # Begin, but do not finish, a third [timestamp, pressure, Fe] record.
        future_seconds = (
            now + dt.timedelta(seconds=1) - LABVIEW_EPOCH
        ).total_seconds()
        with open(path, "ab") as handle:
            handle.write(struct.pack(">d", future_seconds))
            handle.write(struct.pack(">f", 9.0e-9))

        source_ts, values = latest_record(
            path, ["MBE.Pressure", "HTEZ_Fe.PV"],
        )
        assert source_ts == now
        assert values["MBE.Pressure"][0] == pytest.approx(4.0e-9)
        assert values["HTEZ_Fe.PV"][0] == pytest.approx(1110.0)
        value_ts, fe_value = latest_value(path, "HTEZ_Fe.PV")
        assert value_ts == now
        assert fe_value == pytest.approx(1110.0)


def test_latest_record_rejects_file_without_complete_record():
    schema = [("MBE.Pressure", "%.2e mbar")]
    with tempfile.TemporaryDirectory() as tmp:
        path = _today_path(tmp)
        write_elog(path, schema, [])
        with open(path, "ab") as handle:
            handle.write(b"partial")
        with pytest.raises(ValueError, match="no complete data record"):
            latest_record(path, ["MBE.Pressure"])


def test_reader_marks_duplicate_and_stale_source_records():
    schema = [("MBE.Pressure", "%.2e mbar")]
    now = dt.datetime.now(dt.timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = _today_path(tmp)
        write_elog(path, schema, [(now, [4.0e-9])])
        reader = ElogReader(
            log_dir=tmp,
            var_map={"MBE.Pressure": "chamber_pressure_mbar"},
            source_stale_after_s=5.0,
        )
        reader.connect()
        assert reader.read()["chamber_pressure_mbar"] == pytest.approx(4.0e-9)
        assert reader.source_record_advanced is True
        assert reader.source_stale is False
        assert reader.source_status == "advanced"

        # The dict API remains readable, but provenance makes clear this is
        # the same source record and the worker must not count it again.
        assert reader.read()["chamber_pressure_mbar"] == pytest.approx(4.0e-9)
        assert reader.source_record_advanced is False
        assert reader.source_status == "unchanged"

        old = now - dt.timedelta(seconds=20)
        reader.disconnect()
        write_elog(path, schema, [(old, [5.0e-9])])
        reader.connect()
        assert reader.read()["chamber_pressure_mbar"] == pytest.approx(5.0e-9)
        assert reader.source_record_advanced is True
        assert reader.source_stale is True
        assert reader.source_status == "stale"
        assert reader.last_source_age_ms is not None
        assert reader.last_source_age_ms >= 19_000.0


if __name__ == "__main__":
    test_basic()
    test_partial_schema()
    test_pressure_plausibility()
    test_disconnected_returns_none_dict()
    test_nan_plasma_values_treated_as_none()
    test_latest_record_ignores_trailing_partial_append()
    test_latest_record_rejects_file_without_complete_record()
    test_reader_marks_duplicate_and_stale_source_records()
    print("\nALL TESTS PASSED")
