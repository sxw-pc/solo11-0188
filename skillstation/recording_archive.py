"""归档完整性与证书：由 chunk_capture 的物理帧区间与来源传播而来。

完整性只问时轴在期望帧区间内是否连续；侧机画面作为组合证据单列，
绝不伪装成主机原件。旧的上传计数报告与重编码转发路径一律留存备查。
主结果是录屏可复核帧范围与归档完整性，不重判操作安全步骤或考生成绩。
"""
from .chunk_capture import (TRUST_RECOVERED, TRUST_SIDE, collect, digest_of,
                            merge_intervals, missing_ranges, parse_frames,
                            verify_checksum_block)


def assess(rows, expected='0-299', primary_camera=None):
    """按物理帧覆盖评估归档完整性（不再以上传数充数）。"""
    ledger = collect(rows, primary_camera)
    return _build(ledger['camera'], ledger['duplicates'], expected,
                  ledger['segments'], '主机归档', None, rows)


def recover_with_checksums(rows, blocks, expected='0-299', primary_camera=None):
    """授权恢复一：相机签发的原始校验块 + 奇偶冗余重建缺失帧的逐帧摘要。"""
    ledger = collect(rows, primary_camera)
    camera = ledger['camera']
    segments = list(ledger['segments'])
    recovered = []
    for block in blocks:
        verified = verify_checksum_block(block)
        if verified['camera'] != camera:
            raise ValueError('校验块来源相机与主机不符')
        for s in segments:
            if s['camera'] == camera and s['start'] <= verified['end'] and verified['start'] <= s['end']:
                raise ValueError('恢复块不得覆盖已入库主机原件')
        segments.append({'camera': camera, 'start': verified['start'], 'end': verified['end'],
                         'frame_digest': digest_of(verified['frame_digests']),
                         'trust': TRUST_RECOVERED, 'source': f'{camera} 校验块+奇偶冗余恢复',
                         'rep': None, 'uploads': [],
                         'recovered_frames': len(verified['frame_digests'])})
        recovered.append(f"{verified['start']}-{verified['end']}")
    recovery = {'method': '相机校验块+奇偶冗余', 'frames': recovered,
                'source': f'{camera} 签发校验块', 'trust': TRUST_RECOVERED}
    return _build(camera, ledger['duplicates'], expected, segments,
                  '主机归档', recovery, rows)


def combine_side_camera(rows, side_rows, expected='0-299', primary_camera=None):
    """授权恢复二：独立侧机原件作为组合证据，来源单列，不冒充主机原件。"""
    ledger = collect(rows, primary_camera)
    camera = ledger['camera']
    side = collect(side_rows, camera)
    segments = list(ledger['segments'])
    covered_by_side = []
    for s in side['segments']:
        if s['camera'] == camera:
            raise ValueError('侧机补证不得混入主机上传')
        if s['trust'] != TRUST_SIDE:
            raise ValueError('侧机补证须为相机原件')
        segments.append(s)
        covered_by_side.append(f"{s['start']}-{s['end']}")
    recovery = {'method': '独立侧机组合证据', 'frames': covered_by_side,
                'source': '独立侧机原件', 'trust': TRUST_SIDE}
    return _build(camera, ledger['duplicates'] + side['duplicates'], expected,
                  segments, '组合证据', recovery, list(rows) + list(side_rows))


def issue_certificate(assessment):
    """完整归档证书：仅在期望帧区间无缺段时签发。"""
    if assessment['status'] != '完整':
        raise ValueError('存在缺段，不得签发完整归档证书')
    cert = {'type': '完整归档证书',
            'camera': assessment['camera'],
            'range': assessment['expected'],
            'mode': assessment['mode'],
            'segments': [{'frames': s['frames'], 'trust': s['trust'], 'source': s['source']}
                         for s in assessment['segments'] if not s['missing']],
            'duplicates_retained': [d['upload_id'] for d in assessment['duplicates']],
            'legacy_retained': assessment['legacy']}
    cert['certificate_sha'] = digest_of(cert)
    return cert


def archive_receipt(assessment):
    """归档签收单：如实登记可用帧范围、来源等级、缺段与补录需求。"""
    receipt = {'type': '归档签收单',
               'camera': assessment['camera'],
               'expected': assessment['expected'],
               'mode': assessment['mode'],
               'status': assessment['status'],
               'covered': assessment['covered'],
               'holes': assessment['holes'],
               'segments': [{'frames': s['frames'], 'camera': s['camera'],
                             'trust': s['trust'], 'source': s['source'],
                             'missing': s['missing']} for s in assessment['segments']],
               'supplementary_needed': assessment['supplementary_needed'],
               'duplicates_retained': [d['upload_id'] for d in assessment['duplicates']],
               'legacy_retained': assessment['legacy']}
    receipt['receipt_sha'] = digest_of(receipt)
    return receipt


def _build(camera, duplicates, expected, segments, mode, recovery, rows):
    start, end = parse_frames(expected)
    primary = sorted((s for s in segments if s['camera'] == camera),
                     key=lambda s: (s['start'], s['end']))
    others = sorted((s for s in segments if s['camera'] != camera),
                    key=lambda s: (s['start'], s['end'], s['camera']))
    timeline = primary + others if mode == '组合证据' else primary
    covered = merge_intervals((s['start'], s['end']) for s in timeline)
    holes = missing_ranges(covered, start, end)
    return {'camera': camera,
            'expected': expected,
            'mode': mode,
            'chunks': len(primary),
            'status': '完整' if not holes else '缺段',
            'covered': [f'{a}-{b}' for a, b in covered],
            'holes': [f'{a}-{b}' for a, b in holes],
            'segments': _timeline(timeline, holes, camera),
            'duplicates': sorted(duplicates, key=lambda d: d['upload_id']),
            'supplementary_needed': [f'{a}-{b}' for a, b in holes],
            'recovery': recovery,
            'legacy': _legacy_report(rows)}


def _timeline(segments, holes, camera):
    """按帧序逐段列出覆盖来源与可信等级，缺帧范围同样列段。"""
    items = []
    for s in segments:
        item = {'frames': f"{s['start']}-{s['end']}", 'camera': s['camera'],
                'trust': s['trust'], 'source': s['source'],
                'frame_digest': s['frame_digest'],
                'uploads': list(s.get('uploads') or []), 'missing': False}
        if 'recovered_frames' in s:
            item['recovered_frames'] = s['recovered_frames']
        items.append(item)
    for h_start, h_end in holes:
        items.append({'frames': f'{h_start}-{h_end}', 'camera': camera, 'trust': None,
                      'source': None, 'frame_digest': None, 'uploads': [], 'missing': True})
    return sorted(items, key=lambda i: (*parse_frames(i['frames']), i['camera']))


def _legacy_report(rows, expected_chunks=3):
    """旧上传计数报告：仅留存备查，不再作为完整性依据。"""
    count = len({row['upload_id'] for row in rows})
    return {'method': 'upload_id 计数', 'chunks': count,
            'status': '完整' if count >= expected_chunks else '缺段',
            'verdict': '已作废', 'retained': True}
