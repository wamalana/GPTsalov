"""Patch an isolated copy of the existing VPS office dashboard; abort on drift."""
from pathlib import Path
import sys


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Office deployment has drifted: ' + old[:70])
    return text.replace(old, new)


def patch(root):
    root = Path(root)
    monitor = root/'gptsalov/monitor.py'
    js = root/'gptsalov/dashboard/app.js'
    py = monitor.read_text()
    front = js.read_text()
    py = replace(py, '    news = news_snapshot(stamp=stamp)',
        '    from .news_analysis import dashboard as news_dashboard\n'
        '    news = news_dashboard(news_snapshot(stamp=stamp), stamp)')
    py = replace(py, "'kind': 'news_collector' if key == 'news' else 'rule_based_component'",
        "'kind': ('llm_news_context' if news['llm_connected'] else 'news_collector') if key == 'news' else 'rule_based_component'")
    # Current office snapshot expects the old single-slot shape. Keep its view working with v8 lists.
    py = replace(py, "        execution = {'universe':", 
        "        active_slots = s.get('active') or []\n"
        "        if isinstance(active_slots, dict): active_slots = [active_slots]\n"
        "        active = active_slots[0] if active_slots else {}\n"
        "        execution = {'universe':")
    py = replace(py, "(s.get('active') or {}).get('symbol')", "active.get('symbol')")
    py = replace(py, "        active = s.get('active') or {}", "        # active normalized above for the office summary")
    front = replace(front,
        'รวบรวม RSS จาก CoinDesk และ Cointelegraph ทุก 15 นาที ข่าวยังไม่เป็นคะแนนส่งออเดอร์',
        'ข่าว RSS ทุก 15 นาที · AI วิเคราะห์รายชั่วโมง · ประกอบการประเมิน ไม่ข้ามกฎความเสี่ยง')
    old = " put('news-status','ดึงล่าสุด '+when(n.checked_at_ms)+' · '+(labels[n.status]||'ไม่มีข้อมูล'));"
    new = """ const ai=n.analysis||{};
 put('news-status','ดึงล่าสุด '+when(n.checked_at_ms)+' · '+(labels[n.status]||'ไม่มีข้อมูล')+' · AI '+(ai.status||'UNAVAILABLE')+' · '+(ai.model||'—'));
 let summary=$('news-ai-summary');if(!summary){summary=el('p');summary.id='news-ai-summary';$('news-list').before(summary);}
 summary.textContent=n.llm_connected?('วิเคราะห์เมื่อ '+when(ai.observed_ms)+' · '+(ai.summary||'')+' · วิเคราะห์เฉพาะหัวข้อข่าว'):('AI ยังไม่มีผลที่ใช้ได้ · '+(ai.status||'UNAVAILABLE')+(ai.error_code?' · '+ai.error_code:''));"""
    front = replace(front, old, new)
    # Complete every drift check before writing either file.
    monitor.write_text(py)
    js.write_text(front)


if __name__ == '__main__':
    patch(sys.argv[1])
