"""Small labeled retrieval evaluation; no LLM answers or commercial quality claims."""
import json
from pathlib import Path
import time
import sys

from kbtool.core import search
from kbtool.embedding import Encoder, ROOT

CASES = [
    ('Q03', '订单页面翻到后面越来越慢，你当时怎么处理？', ['S04']),
    ('Q04', '420 毫秒降到 165 毫秒，具体是什么指标？', ['S04']),
    ('Q05', '是不是加了 Redis 才把订单列表加速的？', ['S04', 'S05']),
    ('Q06', '用户连点两次提交按钮，会不会创建两张订单？', ['S05']),
    ('Q08', '订单状态刚改完，缓存能保证立即读到最新值吗？', ['S05']),
    ('Q09', '你们为什么没有把工单系统拆成微服务？', ['S06']),
    ('Q10', '原来要跑十几分钟的那批工单提醒任务，怎么提速的？', ['S07']),
    ('Q12', '数据库里已经更新成功，但消息没发出去，这个坑怎么处理？', ['S08']),
    ('Q14', '两个项目里面 Redis 的用途有什么不同？', ['S05', 'S06']),
    ('Q18', '你现在工资多少，期望薪资多少？', ['S10']),
]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    encoder = Encoder()
    package = ROOT / 'output' / 'linzhou-demo.wlkb'
    report = {'description': '10 道单轮检索题，检查 Top5 是否覆盖全部标注章节；不评估生成答案。',
              'cases': []}
    for case_id, query, required in CASES:
        started = time.perf_counter()
        result = search(package, query, encoder, top_k=5)
        sections = [item['section'] for item in result['matches']]
        passed = all(any(section in hit for hit in sections) for section in required)
        report['cases'].append({'id': case_id, 'query': query, 'required_sections': required,
                                'returned_sections': sections, 'all_required_sections_found': passed,
                                'elapsed_ms': round((time.perf_counter() - started) * 1000, 1)})
    report['passed'] = sum(case['all_required_sections_found'] for case in report['cases'])
    report['total'] = len(CASES)
    target = ROOT / 'output' / 'demo-evaluation.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
