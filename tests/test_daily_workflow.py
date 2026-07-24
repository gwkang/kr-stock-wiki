from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/daily-report.yml"
PRE_MARKET_WORKFLOW = ROOT / ".github/workflows/pre-market-report.yml"
MORNING_WORKFLOW = ROOT / ".github/workflows/morning-report.yml"
WIKI_WORKFLOW = ROOT / ".github/workflows/wiki-sync.yml"
WATCHLIST = ROOT / "config/watchlist.json"


def test_daily_report_workflow_runs_post_market_at_2045_kst_and_is_fail_closed():
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    schedules = workflow["on"]["schedule"]
    assert schedules == [{"cron": "45 11 * * 1-5"}]
    assert workflow["permissions"] == {"actions": "write", "contents": "write"}
    assert "KRX_API_KEY: ${{ secrets.KRX_API_KEY }}" in text
    assert "collect-calendar" in text
    assert "calendar-next.json" in text
    assert 'month_day="${BUSINESS_DATE:5:2}${BUSINESS_DATE:8:2}"' in text
    assert '"${calendar_args[@]}"' in text
    assert "scheduled-open" in text
    assert "collect-krx" in text
    assert "collect-nxt" in text
    assert text.count("build-daily-input") == 2
    assert text.count("%6N") == 2
    assert "collect-kind" in text
    assert (
        text.index("Collect complete KIND risk status")
        < text.index("Resolve final analysis timestamp")
        < text.rindex("build-daily-input")
    )
    assert "kr-stock-wiki run" in text
    assert "--watchlist config/watchlist.json" in text
    assert "--nxt-snapshot build/evidence/nxt.json" in text
    assert "kr-stock-wiki lint --wiki wiki" in text
    assert "git add -- wiki" in text
    assert "/actions/workflows/wiki-sync.yml/dispatches" in text
    assert "source_ref" in text
    assert "scripts/sync_wiki.py wiki" not in text
    assert "git clone" not in text
    assert "official-snapshots" in text
    assert "examples/post-market-signals.json" not in text


def test_pre_market_workflow_runs_at_0730_kst_from_previous_official_session():
    assert not MORNING_WORKFLOW.exists()
    text = PRE_MARKET_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"] == [{"cron": "30 22 * * 0-4"}]
    assert workflow["permissions"] == {"actions": "write", "contents": "write"}
    assert "collect-calendar" in text
    assert "if ((10#$month_day >= 1220))" in text
    assert "previous_business_date" in text
    assert "collect-krx-live" not in text
    assert "collect-kis" in text
    assert "KIS_APP_KEY" in text
    assert "KIS_APP_SECRET" in text
    assert "collect-nxt" in text
    assert text.count("build-pre-market-input") == 2
    assert text.count("%6N") == 2
    assert "collect-kind" in text
    assert text.count('--previous-business-date "$PREVIOUS_DATE"') == 3
    assert "--kis-snapshot build/evidence/kis-previous.json" in text
    assert "--nxt-snapshot build/evidence/nxt-previous.json" in text
    assert "kr-stock-wiki run" in text
    assert "kr-stock-wiki lint --wiki wiki" in text
    assert "git add -- wiki" in text
    assert "/actions/workflows/wiki-sync.yml/dispatches" in text
    assert "official-snapshots-pre-market" in text
    assert "09:25" not in text


def test_research_workflows_share_publish_concurrency_group():
    daily = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    pre_market = yaml.load(
        PRE_MARKET_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )

    assert daily["concurrency"]["group"] == "kr-stock-wiki-research-publish"
    assert pre_market["concurrency"]["group"] == "kr-stock-wiki-research-publish"


@pytest.mark.parametrize("workflow_path", [WORKFLOW, PRE_MARKET_WORKFLOW])
def test_research_workflow_pins_every_external_action_to_commit_sha(workflow_path):
    text = workflow_path.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*([^\s#]+)", text)

    assert uses
    assert all(
        entry.startswith("./") or re.fullmatch(r"[^@]+@[0-9a-f]{40}", entry)
        for entry in uses
    )


def test_daily_watchlist_is_explicit_real_configuration_not_sample_data():
    payload = json.loads(WATCHLIST.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["source"] == "user-watchlist"
    assert 1 <= len(payload["stocks"]) <= 20
    assert len({stock["ticker"] for stock in payload["stocks"]}) == len(
        payload["stocks"]
    )
    assert all("모의" not in stock["name"] for stock in payload["stocks"])


def test_wiki_sync_also_publishes_validated_wiki_changes_from_main():
    workflow = yaml.load(
        WIKI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )

    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["push"]["paths"] == ["wiki/**"]
    assert "source_ref" in workflow["on"]["workflow_dispatch"]["inputs"]
    publish = workflow["jobs"]["publish"]
    assert workflow["concurrency"]["group"] == "github-wiki-publish"
    checkout = publish["steps"][0]
    assert checkout["with"]["fetch-depth"] == "0"
    pin_step = publish["steps"][1]
    assert pin_step["env"]["REQUESTED_REF"] == "${{ inputs.source_ref || github.sha }}"
    assert "^[0-9a-f]{40}$" in pin_step["run"]
    assert "git merge-base --is-ancestor" in pin_step["run"]
    assert "git checkout --detach" in pin_step["run"]
