from io import BytesIO

from openpyxl import Workbook

from app.manually_execute_script.run_reliability_benchmark import (
    BenchmarkCase,
    read_dataset_cases,
    read_special_cases,
)


def test_dataset_reader_accepts_xlsx_named_csv_and_deduplicates(tmp_path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["URL", "互动量", "评论数"])
    sheet.append(["https://www.bilibili.com/video/BV1xx411c7mD", 12, 3])
    sheet.append(["https://www.bilibili.com/video/BV1xx411c7mD", 12, 3])
    content = BytesIO()
    workbook.save(content)
    (tmp_path / "有评论数据.csv").write_bytes(content.getvalue())

    cases = read_dataset_cases(tmp_path)

    assert len(cases) == 2
    assert {case.operation for case in cases} == {"comments", "interactions"}
    assert cases[0].expected_comments == 3
    assert cases[0].expected_interactions == 12


def test_special_cases_cycle_urls_for_each_separate_interface(tmp_path) -> None:
    config = tmp_path / "special.json"
    config.write_text(
        '{"haokan":["https://haokan.baidu.com/v?vid=1","https://haokan.baidu.com/v?vid=2"]}',
        encoding="utf-8",
    )

    cases = read_special_cases(config, 3)

    assert len(cases) == 6
    assert [case.operation for case in cases] == [
        "comments", "comments", "comments", "interactions", "interactions", "interactions"
    ]
    assert [case.url.rsplit("=", 1)[-1] for case in cases[:3]] == ["1", "2", "1"]
    assert len({case.case_id for case in cases}) == 6


def test_case_id_is_stable_and_operation_specific() -> None:
    comments = BenchmarkCase("dataset", "weibo", "comments", "https://m.weibo.cn/detail/1", 1)
    interactions = BenchmarkCase("dataset", "weibo", "interactions", "https://m.weibo.cn/detail/1", 1)

    assert comments.case_id == comments.case_id
    assert comments.case_id != interactions.case_id
