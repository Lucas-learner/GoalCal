"""Tests for generalized pre-draft goal intake & title distillation."""

import asyncio
from dataclasses import replace
from datetime import date, timedelta
import json
import uuid

import httpx2
import pytest

from goalcal import auth, db, migrations, storage
from goalcal.llm import intake as intake_mod
from goalcal.web import http as web_http

ORIGIN = 'https://goalcal.zhang.sarl'


def test_rule_intake_generalizes_non_sports_scenarios():
    today = date(2026, 11, 17)

    # 1. Tight exam + near-zero baseline -> calibrates fixed vs flexible & scope
    ielts = intake_mod.assess_intake_rules(
        '下个月要考雅思 7.5，平时几乎不看英文',
        today + timedelta(days=25),
        today=today,
        lang='zh',
    )
    assert ielts['needs_calibration'] is True
    assert ielts['conflict_kind'] == 'scope_vs_time'
    assert '我想' not in ielts['suggested_title']
    assert '平时几乎不看英文' not in ielts['suggested_title']
    q_by_id = {q['id']: q for q in ielts['questions']}
    assert any(opt['id'] == 'fixed' for opt in q_by_id['deadline_type']['options'])
    assert any(opt.get('suggested_deadline') for opt in q_by_id['direction']['options'])

    # 1b. Ambitious goal WITHOUT baseline stated -> must ask baseline first!
    no_baseline = intake_mod.assess_intake_rules(
        '我想一个月后能跑全马',
        today + timedelta(days=30),
        today=today,
        lang='zh',
    )
    assert no_baseline['needs_calibration'] is True
    assert no_baseline['conflict_kind'] == 'missing_baseline'
    assert no_baseline['suggested_title'] == '跑全马'
    assert no_baseline['questions'][0]['id'] == 'baseline'
    assert len(no_baseline['questions'][0]['options']) == 3

    # 2. Multi-module software build in very short window -> scope calibration
    saas = intake_mod.assess_intake_rules(
        'Build a full SaaS with auth, billing, community chat, and mobile app',
        today + timedelta(days=7),
        today=today,
        lang='en',
    )
    assert saas['needs_calibration'] is True
    assert saas['conflict_kind'] == 'scope_vs_time'
    assert saas['suggested_title']

    # 3. Broad skill without baseline or deliverable -> vague_target calibration
    piano = intake_mod.assess_intake_rules(
        '我想学会弹钢琴',
        today + timedelta(days=30),
        today=today,
        lang='zh',
    )
    assert piano['needs_calibration'] is True
    assert piano['conflict_kind'] == 'vague_target'
    assert piano['suggested_title'] == '学会弹钢琴'

    # 4. Concrete realistic task -> fast path (no calibration interruption)
    expenses = intake_mod.assess_intake_rules(
        '两周内整理完过去两年的报销发票并提交财务',
        today + timedelta(days=14),
        today=today,
        lang='zh',
    )
    assert expenses['needs_calibration'] is False
    assert expenses['suggested_title'] == '整理完过去两年的报销发票并提交财务'


def test_goal_intake_api_with_llm_and_calibrated_draft(tmp_path, monkeypatch):
    path = tmp_path / 'intake.db'
    monkeypatch.setattr(db, 'settings', replace(db.settings, db_url=f'sqlite:///{path}'))
    monkeypatch.setattr(storage, 'database_path', lambda url=None: path)
    monkeypatch.setattr(migrations, 'database_path', lambda url=None: path)
    db.init_db()

    user_name = 'u_' + uuid.uuid4().hex[:10]
    auth.create_user(user_name, 'test-password-long', tz='Asia/Shanghai', admin=True)
    token = auth.login(user_name, 'test-password-long')

    class _MockIntakeClient:
        ready = True

        def complete_json(self, system, user_prompt, **_kwargs):
            payload = json.loads(user_prompt)
            assert 'SaaS' in payload['description']
            return {
                'needs_calibration': True,
                'conflict_kind': 'scope_vs_time',
                'suggested_title': 'SaaS 核心 MVP 上线',
                'suggested_outcome': '核心流程可用并上线验证',
                'reason': '10 天内完成完整系统与多端客户端跨度过大，先确认期限性质和首期范围更稳妥。',
                'questions': [
                    {
                        'id': 'deadline_type',
                        'prompt': '10 天后是固定上线/路演日吗？',
                        'options': [
                            {'id': 'fixed', 'label': '固定路演日'},
                            {'id': 'flexible', 'label': '可顺延上线'},
                        ],
                    },
                    {
                        'id': 'direction',
                        'prompt': '希望怎么调整？',
                        'options': [
                            {
                                'id': 'mvp',
                                'label': '砍为单一核心流程 MVP',
                                'suggested_title': 'SaaS 核心 MVP 上线',
                                'adjusted_scope': '只做核心登录与单一核心闭环，暂缓社区和移动端。',
                            },
                            {
                                'id': 'extend',
                                'label': '延后到两个月完整上线',
                                'suggested_title': 'SaaS 完整版上线',
                                'suggested_deadline': (date.today() + timedelta(days=75)).isoformat(),
                            },
                        ],
                    },
                ],
            }

    monkeypatch.setattr(intake_mod, 'settings', replace(intake_mod.settings, llm_enabled=True))
    monkeypatch.setattr(intake_mod.router, 'client', lambda layer: _MockIntakeClient())

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=web_http.api),
            base_url=ORIGIN,
            cookies={'goalcal_session': token},
        ) as c:
            res = await c.post(
                '/goal-intake',
                json={'description': '10天做出带支付、社区、移动端和AI的完整SaaS上线', 'deadline': None},
                headers={'Origin': ORIGIN, 'Idempotency-Key': 'intake-saas-1234', 'X-GoalCal-Lang': 'zh'},
            )
            assert res.status_code == 200
            data = res.json()
            assert data['needs_calibration'] is True
            assert data['suggested_title'] == 'SaaS 核心 MVP 上线'
            assert data['source'] == 'llm'

            draft = await c.post(
                '/goal-drafts',
                json={
                    'title': data['suggested_title'],
                    'raw': '只做核心登录与单一核心闭环，暂缓社区和移动端。',
                    'deadline': None,
                },
                headers={'Origin': ORIGIN, 'Idempotency-Key': 'draft-saas-1234', 'X-GoalCal-Lang': 'zh'},
            )
            assert draft.status_code == 200
            assert draft.json()['title'] == 'SaaS 核心 MVP 上线'

            revised = await c.post(
                '/goal-drafts',
                json={
                    'title': data['suggested_title'],
                    'raw': '只做核心登录与单一核心闭环，暂缓社区和移动端。',
                    'instruction': '每步拆短到 20 分钟内',
                    'current_steps': draft.json()['steps'],
                    'current_outline': draft.json()['outline'],
                    'deadline': None,
                },
                headers={'Origin': ORIGIN, 'Idempotency-Key': 'draft-revise-1234', 'X-GoalCal-Lang': 'zh'},
            )
            assert revised.status_code == 200
            assert all(s['est_min'] <= 20 for s in revised.json()['steps'])

    asyncio.run(run())
