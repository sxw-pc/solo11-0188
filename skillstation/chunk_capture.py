"""物理帧覆盖采录：按相机原始帧号而非上传号、接收顺序或视频时长构成连续覆盖。

上传只携带证据：相机帧序、转发路由、编码变换与原帧指纹。同一物理帧的
重编码转发是重复证据而非新覆盖；不同视角相机的同刻画面保留来源区分，
绝不并入主机时轴。已入库分片的原帧摘要一经记录，迟到分片只能佐证，
不得覆盖。
"""
import hashlib, json

TRUST_ORIGINAL = '主机原件'
TRUST_RELAYED = '转发原件'
TRUST_REENCODED = '重编码转发件'
TRUST_RECOVERED = '校验块恢复件'
TRUST_SIDE = '侧机原件'

_TRUST_RANK = {TRUST_REENCODED: 1, TRUST_RELAYED: 2, TRUST_RECOVERED: 2,
               TRUST_ORIGINAL: 3, TRUST_SIDE: 3}


def parse_frames(spec):
    """把 '起-止' 闭区间帧段解析为 (start, end)。"""
    try:
        start, end = (int(part) for part in str(spec).split('-'))
    except (TypeError, ValueError):
        raise ValueError(f'无效帧段: {spec!r}') from None
    if start < 0 or end < start:
        raise ValueError(f'无效帧段: {spec!r}')
    return start, end


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest_of(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalize_upload(row, sequence=0):
    """把一条上传记录规范为物理帧区间与来源链。"""
    start, end = parse_frames(row['frames'])
    camera = row.get('camera') or row.get('device')
    route = list(row.get('route') or [camera])
    if not camera or route[0] != camera:
        raise ValueError(f"转发链起点 {route[:1]} 与来源相机 {camera} 不符")
    return {
        'upload_id': row['upload_id'],
        'device': row.get('device'),
        'camera': camera,
        'start': start,
        'end': end,
        'raw_sha': row.get('raw_sha'),
        'frame_digest': row.get('frame_digest') or row.get('raw_sha'),
        'route': route,
        'encoding': row.get('encoding') or 'original',
        'sequence': sequence,
    }


def classify(upload, primary_camera):
    """按编码变换、转发路由与来源相机判定可信等级。"""
    if upload['encoding'] != 'original':
        return TRUST_REENCODED
    if upload['camera'] != primary_camera:
        return TRUST_SIDE
    if len(upload['route']) > 2:
        return TRUST_RELAYED
    return TRUST_ORIGINAL


def _source_label(upload, trust):
    if trust == TRUST_ORIGINAL:
        return f"{upload['camera']} 直传原件"
    if trust == TRUST_RELAYED:
        return f"{upload['camera']} 经 {'>'.join(upload['route'])} 转发原件"
    if trust == TRUST_REENCODED:
        return f"{upload['device']} 重编码转发（{upload['encoding']}）"
    if trust == TRUST_SIDE:
        return f"{upload['camera']} 侧机原件"
    return upload['device'] or upload['camera']


def _overlaps(a, b):
    return a['start'] <= b['end'] and b['start'] <= a['end']


def _new_segment(up):
    return {'camera': up['camera'], 'start': up['start'], 'end': up['end'],
            'frame_digest': up['frame_digest'], 'trust': up['trust'],
            'source': _source_label(up, up['trust']), 'rep': up,
            'uploads': [up['upload_id']]}


def _promote(segment, up):
    """用更可信的原件替换代表源；已记录的原帧摘要保持不变。"""
    segment['trust'] = up['trust']
    segment['source'] = _source_label(up, up['trust'])
    segment['rep'] = up


def _duplicate_record(up, segment):
    return {'upload_id': up['upload_id'], 'device': up['device'], 'camera': up['camera'],
            'frames': f"{up['start']}-{up['end']}", 'route': up['route'],
            'encoding': up['encoding'], 'raw_sha': up['raw_sha'],
            'frame_digest': up['frame_digest'], 'trust': up['trust'],
            'duplicate_of': segment['rep']['upload_id'],
            'reason': '同一物理帧的重编码转发' if up['encoding'] != 'original' else '同一物理帧的重复转发'}


def _primary_camera(uploads):
    totals, order = {}, []
    for up in uploads:
        if up['camera'] not in totals:
            totals[up['camera']] = 0
            order.append(up['camera'])
        if up['encoding'] == 'original':
            totals[up['camera']] += up['end'] - up['start'] + 1
    return max(order, key=lambda c: (totals[c], -order.index(c)))


def collect(rows, primary_camera=None):
    """按接收顺序把上传汇入物理帧台账，返回 {'camera', 'segments', 'duplicates'}。

    同一物理区间以首次记录的原帧指纹为准：指纹相同的迟到分片只保留为
    重复转发证据（代表源可被更可信的原件取代，指纹本身不被改写）；指纹
    不同的迟到分片直接拒绝，不得覆盖已入库摘要。同一 upload_id 内容一致
    的重放幂等，内容不一致即拒绝。
    """
    uploads = [normalize_upload(row, i) for i, row in enumerate(rows)]
    if not uploads:
        raise ValueError('empty upload list')
    camera = primary_camera or _primary_camera(uploads)
    segments, duplicates, seen = [], [], {}
    for up in uploads:
        up['trust'] = classify(up, camera)
        key = {k: v for k, v in up.items() if k != 'sequence'}
        if up['upload_id'] in seen:
            if seen[up['upload_id']] != key:
                raise ValueError(f"上传 {up['upload_id']} 重报内容不一致")
            continue
        seen[up['upload_id']] = key
        match = next((s for s in segments if s['camera'] == up['camera']
                      and s['start'] == up['start'] and s['end'] == up['end']), None)
        if match is None:
            for s in segments:
                if s['camera'] == up['camera'] and _overlaps(s, up):
                    raise ValueError(f"分片 {up['upload_id']} 与已入库 {s['camera']} "
                                     f"{s['start']}-{s['end']} 物理帧冲突")
            segments.append(_new_segment(up))
            continue
        if match['frame_digest'] != up['frame_digest']:
            raise ValueError(f"迟到分片 {up['upload_id']} 不得覆盖 {up['camera']} "
                             f"{up['start']}-{up['end']} 已入库原帧摘要")
        if _TRUST_RANK[up['trust']] > _TRUST_RANK[match['trust']]:
            old = match['rep']
            _promote(match, up)
            duplicates.append(_duplicate_record(old, match))
        else:
            duplicates.append(_duplicate_record(up, match))
        match['uploads'] = sorted(set(match['uploads']) | {up['upload_id']})
    return {'camera': camera, 'segments': segments, 'duplicates': duplicates}


def distinct_chunks(rows, primary_camera=None):
    """去重后的物理覆盖分片数（按相机原始帧区间，不按上传号）。"""
    return len(collect(rows, primary_camera)['segments'])


def merge_intervals(intervals):
    """合并相邻或重叠的物理帧区间。"""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def missing_ranges(covered, start, end):
    """在期望区间 [start, end] 内逐段列出未被覆盖的缺帧范围。"""
    holes, cursor = [], start
    for c_start, c_end in merge_intervals(covered):
        if c_end < cursor:
            continue
        if c_start > cursor:
            holes.append((cursor, c_start - 1))
        cursor = max(cursor, c_end + 1)
        if cursor > end:
            break
    if cursor <= end:
        holes.append((cursor, end))
    return holes


def checksum_block(camera, start, end, frame_digests):
    """相机签发的原始校验块：逐帧摘要 + 奇偶冗余 + 签发签名。"""
    digests = list(frame_digests)
    if len(digests) != end - start + 1:
        raise ValueError('逐帧摘要数量与帧段不符')
    block_sha = digest_of({'camera': camera, 'frames': [start, end], 'digests': digests})
    parity = {'even': digest_of([d for d, n in zip(digests, range(start, end + 1)) if n % 2 == 0]),
              'odd': digest_of([d for d, n in zip(digests, range(start, end + 1)) if n % 2 == 1])}
    return {'camera': camera, 'frames': f'{start}-{end}', 'frame_digests': digests,
            'block_sha': block_sha, 'parity': parity,
            'signature': digest_of({'camera': camera, 'block_sha': block_sha})}


def verify_checksum_block(block):
    """重建逐帧原始摘要并核验块摘要、奇偶冗余与签发签名。"""
    start, end = parse_frames(block['frames'])
    expect = checksum_block(block['camera'], start, end, block.get('frame_digests') or [])
    for key in ('block_sha', 'parity', 'signature'):
        if block.get(key) != expect[key]:
            raise ValueError(f'校验块{key}核验失败')
    return {'camera': block['camera'], 'start': start, 'end': end,
            'frame_digests': list(block['frame_digests'])}
