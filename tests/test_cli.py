import pytest

from qlib_sync import cli


def test_dataset_overview_reports_latest_date_per_dataset(tmp_path):
    (tmp_path / "cn_data" / "calendars").mkdir(parents=True)
    (tmp_path / "cn_data" / "calendars" / "day.txt").write_text("2026-07-02\n2026-07-03\n")
    (tmp_path / "cn_data" / "calendars" / "day_future.txt").write_text("2026-12-31\n")
    (tmp_path / "not_a_dataset").mkdir()

    assert cli.dataset_overview(tmp_path) == [{"name": "cn_data", "freq": "day", "latest": "2026-07-03"}]


def test_unknown_command_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["nope"])
    assert exc.value.code == 2


def test_cn_help_is_routed_to_the_cn_parser(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["cn", "--help"])
    assert exc.value.code == 0
    assert "health-check" in capsys.readouterr().out
