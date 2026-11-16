"""Phase 4 tests - offline: the one-command flow calls each step in order."""
from types import SimpleNamespace

from search_engine import pipeline
from search_engine.payers import from_registry

PAYERS = from_registry([{"payer_name": "Centene", "hint_host": "centene.com", "affiliated_domains": []}])


def test_crawler_is_run_unchanged_for_the_payer():
    seen = {}
    def runner(cmd, cwd):
        seen.update(cmd=cmd, cwd=cwd)
        return SimpleNamespace(returncode=0)
    assert pipeline.run_crawler(PAYERS[0], runner) == 0
    assert seen["cmd"][1:] == ["payer_policy_crawler.py", "--homepage", "centene.com"]
    assert seen["cwd"] == str(pipeline.PROJECT_ROOT)


def test_run_calls_crawl_collect_verify_then_reports(monkeypatch):
    steps = []
    monkeypatch.setattr(pipeline, "run_crawler", lambda p, r: steps.append(("crawl", p.name)))
    monkeypatch.setattr(pipeline, "collect", lambda p, src, lim, mq, refresh=False, archives=None: steps.append(("collect", src, lim)))
    monkeypatch.setattr(pipeline, "verify", lambda p, out, mx: steps.append(("verify", mx)))
    monkeypatch.setattr(pipeline, "write_reports", lambda ps, out: steps.append(("reports",)) or ("a", "b", "c"))
    pipeline.run(PAYERS, crawl=True, searxng=True, limit=100, verify_max=-1)
    assert steps == [("crawl", "Centene"), ("collect", "all", 100), ("verify", None), ("reports",)]


def test_run_without_crawl_or_verification(monkeypatch):
    steps = []
    monkeypatch.setattr(pipeline, "run_crawler", lambda p, r: steps.append("crawl"))
    monkeypatch.setattr(pipeline, "collect", lambda p, src, lim, mq, refresh=False, archives=None: steps.append(f"collect:{src}"))
    monkeypatch.setattr(pipeline, "verify", lambda p, out, mx: steps.append("verify"))
    monkeypatch.setattr(pipeline, "write_reports", lambda ps, out: steps.append("reports"))
    pipeline.run(PAYERS, verify_max=0)
    assert steps == ["collect:archive", "reports"]


def test_run_log_copies_output(tmp_path, monkeypatch):
    import search_engine.config as cfg
    from search_engine.__main__ import run_log
    monkeypatch.setattr(cfg, "LOGS_DIR", tmp_path / "logs")
    with run_log("verify", ["verify", "--payer", "X"]) as rl:
        print("hello from the run")
    text = rl.path.read_text(encoding="utf-8")
    assert "hello from the run" in text and "--payer X" in text and "ended" in text
    assert (tmp_path / "logs" / "latest_verify.log").read_text(encoding="utf-8") == text
