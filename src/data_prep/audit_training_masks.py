"""Read-only training-mask audit. Model disagreements are review candidates, not truth."""
import argparse
import base64
import csv
import hashlib
import html
import io
import json
import random
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from tqdm.auto import tqdm

NAMES = ['apple', 'tomato', 'bruise_discoloration', 'rot_mold_decay', 'surface_damage']
VERSION = 1


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def raster(points, width, height):
    mask = np.zeros((height, width), np.uint8)
    p = np.rint(np.asarray(points) * [width, height]).astype(np.int32)
    cv2.fillPoly(mask, [p], 1)
    return mask.astype(bool)


def iou(a, b):
    union = np.count_nonzero(a | b)
    return np.count_nonzero(a & b) / union if union else 0.0


def read_annotations(path, width, height):
    annotations, flags = [], []
    if not path.is_file():
        return [], [dict(reason='missing_label_file', line=0, weight=10)]
    for line, text in enumerate(path.read_text().splitlines(), 1):
        try:
            v = np.asarray(text.split(), dtype=float)
            if len(v) < 7 or (len(v) - 1) % 2 or not np.isfinite(v).all():
                raise ValueError('Invalid polygon values')
            if v[0] != int(v[0]) or not 0 <= v[0] < len(NAMES):
                raise ValueError('Invalid class ID')
            if np.any(v[1:] < 0) or np.any(v[1:] > 1):
                raise ValueError('Coordinates outside image')
            points = v[1:].reshape(-1, 2)
            if len(np.unique(points, axis=0)) < 3:
                raise ValueError('Fewer than three distinct vertices')
            mask = raster(points, width, height)
            area = int(mask.sum())
            annotations.append(dict(class_id=int(v[0]), points=points.tolist(), line=line, area=area))
            if area < 16:
                flags.append(dict(reason='tiny_or_empty_mask', line=line, pixels=area, weight=3))
        except ValueError as error:
            flags.append(dict(reason='invalid_polygon', line=line, detail=str(error), weight=10))
    return annotations, flags


def inspect_geometry(annotations, width, height, fruit):
    masks = [raster(a['points'], width, height) for a in annotations]
    body = np.zeros((height, width), bool)
    fruit_id = NAMES.index(fruit)
    flags = []
    for a, mask in zip(annotations, masks):
        if a['class_id'] == fruit_id:
            body |= mask
        elif a['class_id'] < 2:
            flags.append(dict(reason='unexpected_fruit_class', line=a['line'], weight=6))
    if not body.any():
        flags.append(dict(reason='missing_fruit_mask', weight=10))
    for i, (a, mask) in enumerate(zip(annotations, masks)):
        if a['class_id'] >= 2 and body.any() and mask.any():
            outside = np.count_nonzero(mask & ~body) / mask.sum()
            if outside > 0.1:
                flags.append(dict(reason='defect_outside_fruit', line=a['line'], outside_fraction=round(float(outside), 4), weight=5))
        for j in range(i):
            b = annotations[j]
            if a['class_id'] < 2 or b['class_id'] < 2:
                continue  # Fruit/defect nesting is expected.
            overlap = iou(mask, masks[j])
            if a['class_id'] == b['class_id'] and overlap >= 0.95:
                flags.append(dict(reason='near_duplicate_defect_masks', lines=[b['line'], a['line']], iou=round(overlap, 4), weight=5))
            elif a['class_id'] != b['class_id'] and overlap >= 0.6:
                flags.append(dict(reason='overlapping_defect_classes', lines=[b['line'], a['line']], iou=round(overlap, 4), weight=4))
    return flags, masks, body


def compare_predictions(annotations, masks, body, predictions, width, height):
    flags, proposals, matched = [], [], set()
    for pred in predictions:
        cls = pred['class_id']
        if cls < 2 or pred['confidence'] < 0.35:
            continue
        mask = raster(pred['points'], width, height)
        if not mask.any():
            continue
        containment = float(np.count_nonzero(mask & body) / mask.sum()) if body.any() else 0
        if containment < 0.8:
            continue
        candidates = [(iou(mask, gm), i) for i, (a, gm) in enumerate(zip(annotations, masks)) if a['class_id'] >= 2]
        best, idx = max(candidates, default=(0, -1))
        same = [(score, i) for score, i in candidates if annotations[i]['class_id'] == cls]
        same_score, same_idx = max(same, default=(0, -1))
        if same_score >= 0.3:
            matched.add(same_idx)
        reason = None
        if pred['confidence'] >= 0.5 and best < 0.1:
            reason = 'possible_missing_defect'
        elif pred['confidence'] >= 0.5 and best >= 0.5 and annotations[idx]['class_id'] != cls and same_score < 0.3:
            reason = 'model_class_disagreement'
        elif pred['confidence'] >= 0.5 and 0.1 <= same_score < 0.5:
            reason = 'model_boundary_disagreement'
        if reason:
            flags.append(dict(reason=reason, suggested_class=NAMES[cls], confidence=round(pred['confidence'], 4), best_iou=round(best, 4), weight=5 if reason == 'possible_missing_defect' else 2))
            proposals.append(dict(**pred, reason=reason, approved=False, source='trained_yolo', associated_line=annotations[same_idx]['line'] if same_idx >= 0 else None))
    for i, a in enumerate(annotations):
        if a['class_id'] >= 2 and i not in matched:
            flags.append(dict(reason='model_missed_annotation', line=a['line'], weight=1))
    return flags, proposals


def prediction_rows(result):
    if result.masks is None or result.boxes is None:
        return []
    rows = []
    for cls, confidence, points in zip(result.boxes.cls.cpu().tolist(), result.boxes.conf.cpu().tolist(), result.masks.xyn):
        if len(points) >= 3:
            rows.append(dict(class_id=int(cls), confidence=float(confidence), points=np.clip(points, 0, 1).tolist()))
    return rows


def sam_proposal_for_box(sam, image, box, annotation, width, height, device):
    """One prompt at a time prevents dropped masks from shifting class assignments."""
    if box[2] <= box[0] or box[3] <= box[1]:
        return dict(points=None, candidate_count=0, reason='degenerate_box')
    results = sam.predict(str(image), bboxes=[box], device=device, verbose=False, save=False)
    candidates = []
    for result in results:
        if result.masks is None:
            continue
        for points in result.masks.xyn:
            points = np.asarray(points, dtype=float)
            if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
                continue
            points = np.clip(points, 0, 1)
            mask = raster(points, width, height)
            if mask.any():
                candidates.append((iou(raster(annotation['points'], width, height), mask), points.tolist()))
    if not candidates:
        return dict(points=None, candidate_count=0, reason='no_mask')
    overlap, points = max(candidates, key=lambda item:item[0])
    return dict(points=points, candidate_count=len(candidates), prompt_iou=round(overlap, 4))


def image_card(dataset, record):
    with Image.open(dataset / 'train/images' / record['file']) as source:
        image = source.convert('RGB')
    image.thumbnail((450, 450))
    panels = []
    palette = {0: '#2670bb', 1: '#2670bb', 2: '#efad16', 3: '#de3333', 4: '#8e47dd'}
    for label, shapes in [('Existing annotations', record['annotations']), ('AI proposals (unapproved)', record['proposals'])]:
        panel = image.copy()
        draw = ImageDraw.Draw(panel)
        for shape in shapes:
            pts = [(p[0] * panel.width, p[1] * panel.height) for p in shape['points']]
            if len(pts) >= 3:
                draw.line(pts + [pts[0]], fill=palette[shape['class_id']], width=2)
        buf = io.BytesIO()
        panel.save(buf, format='JPEG', quality=75)
        panels.append(f'<figure><img src="data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"><figcaption>{label}</figcaption></figure>')
    name = html.escape(record['file'])
    reasons = html.escape(', '.join(sorted({f['reason'] for f in record['flags']})) or 'Random unflagged sample')
    return f'<article data-file="{name}" data-fruit="{record["fruit"]}"><h3>{name}</h3><p>Priority {record["score"]} | {reasons}</p><div class="panels">'+''.join(panels)+f'</div><label>Review decision <select><option>Unreviewed</option><option>Keep existing</option><option>Needs correction</option><option>Ambiguous</option></select></label><input placeholder="Review notes, proposed class, or mask line"></article>'


def write_reports(dataset, output, records, metadata, review_limit=100):
    ranked = sorted(records, key=lambda r: (-r['score'], r['file']))
    with (output / 'review_queue.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['file', 'fruit', 'priority', 'flags', 'review_status', 'notes'])
        writer.writeheader()
        for r in ranked:
            writer.writerow(dict(file=r['file'], fruit=r['fruit'], priority=r['score'], flags=';'.join(sorted({x['reason'] for x in r['flags']})), review_status='unreviewed', notes=''))
    (output / 'audit_records.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
    (output / 'proposed_masks.jsonl').write_text(''.join(json.dumps(dict(file=r['file'], fruit=r['fruit'], **p)) + '\n' for r in records for p in r['proposals']))
    reasons = Counter(f['reason'] for r in records for f in r['flags'])
    metadata.update(images_checked=len(records), images_flagged=sum(bool(r['flags']) for r in records), reasons=dict(reasons), proposals=sum(len(r['proposals']) for r in records), status='complete')
    (output / 'summary.json').write_text(json.dumps(metadata, indent=2))
    chosen = [r for r in ranked if r['flags']][:review_limit]
    clean = [r for r in records if not r['flags']]
    chosen += random.Random(0).sample(clean, min(20, len(clean)))
    cards = '\n'.join(image_card(dataset, r) for r in chosen)
    header = '<!doctype html><meta charset="utf-8"><title>Training mask review</title><style>body{font:15px Arial;background:#f4f5f7;margin:24px;color:#172333}article{background:white;padding:18px;margin:18px 0;border-radius:10px}h3{overflow-wrap:anywhere}.panels{display:flex;flex-wrap:wrap}figure{margin:8px}img{max-width:100%}input{width:50%;padding:8px;margin:8px}button,select{padding:8px}figcaption{padding:8px}</style><h1>Training mask review</h1><p>AI disagreements are not verified errors. Bruise=yellow, rot=red, surface damage=purple, fruit=blue. Suggestions are unapproved. This page shows the first '+str(review_limit)+' flagged images and up to 20 random unflagged images; the CSV contains every checked image. No dataset labels are changed.</p><p><select id="fruit"><option value="all">Both fruits</option><option>apple</option><option>tomato</option></select> <button id="download">Download review decisions</button></p>'
    script = '''<script>
const cards=[...document.querySelectorAll('article')];
const key='mask-audit-'+'''+json.dumps(metadata['cache_namespace'])+''';
const saved=JSON.parse(localStorage.getItem(key)||'{}');
for(const c of cards){const s=c.querySelector('select'),n=c.querySelector('input'),v=saved[c.dataset.file];if(v){s.value=v.status;n.value=v.notes;}for(const e of [s,n])e.addEventListener('change',()=>{saved[c.dataset.file]={status:s.value,notes:n.value};localStorage.setItem(key,JSON.stringify(saved));});}
document.querySelector('#fruit').onchange=e=>cards.forEach(c=>c.hidden=e.target.value!=='all'&&c.dataset.fruit!==e.target.value);
document.querySelector('#download').onclick=()=>{const rows=cards.map(c=>({file:c.dataset.file,status:c.querySelector('select').value,notes:c.querySelector('input').value}));const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(rows,null,2)],{type:'application/json'}));a.download='review_decisions.json';a.click();};
</script>'''
    (output / 'review.html').write_text(header + cards + script)
    (output / 'README.txt').write_text('Training-only read-only audit. Open review.html in a browser. Review flags and a random sample before editing any labels. proposed_masks.jsonl contains unapproved normalized polygons, not training labels. No automatic apply step. Model misses may reflect a model error, not an annotation error. A model trained on these images cannot certify their labels. SAM 2 boundary proposals inherit a supplied class and do not verify defect type. All weights and thresholds are recorded in summary.json.\n')
    print(json.dumps({k:metadata[k] for k in ['images_checked', 'images_flagged', 'proposals', 'reasons']}, indent=2))
    return metadata


def run_audit(dataset, output, checkpoint=None, device='cpu', imgsz=1024, max_images=0, sam2_path=None, sam_limit=40, review_limit=100):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if output == dataset or dataset in output.parents:
        raise ValueError('Audit output must be outside the dataset')
    manifest_path = dataset / 'manifest.json'
    rows = [r for r in json.loads(manifest_path.read_text()) if r['split'] == 'train']
    if max_images:
        random.Random(0).shuffle(rows)
        rows = rows[:max_images]
    for r in rows:
        if Path(r['file']).name != r['file']:
            raise ValueError('Unsafe manifest filename')
    output.mkdir(parents=True, exist_ok=True)
    source_snapshot = {r['file']:digest(dataset / 'train/labels' / Path(r['file']).with_suffix('.txt')) if (dataset / 'train/labels' / Path(r['file']).with_suffix('.txt')).exists() else None for r in rows}
    checkpoint = Path(checkpoint).resolve() if checkpoint else None
    if checkpoint and not checkpoint.is_file():
        raise FileNotFoundError(checkpoint)
    package_version = None
    if checkpoint or sam2_path:
        import ultralytics
        package_version = ultralytics.__version__
    key = dict(version=VERSION, ultralytics_version=package_version, manifest_sha256=digest(manifest_path), checkpoint_sha256=digest(checkpoint) if checkpoint else None, imgsz=imgsz, prediction_conf=0.2, candidate_conf=0.5)
    namespace = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
    cache = output / 'prediction_cache' / namespace
    cache.mkdir(parents=True, exist_ok=True)
    metadata = dict(**key, cache_namespace=namespace, split='train', dataset=str(dataset), checkpoint=str(checkpoint) if checkpoint else None, device=device, sampled=bool(max_images), no_labels_changed=True, started_at=time.strftime('%Y-%m-%dT%H:%M:%S'), status='running', sam2_path=str(sam2_path) if sam2_path else None, sam_limit=sam_limit, thresholds=dict(outside_fruit=0.1, duplicate_iou=0.95, class_overlap_iou=0.6, tiny_pixels=16, candidate_conf=0.5), limitations=['Same-model predictions can repeat annotation and model errors.', 'Unflagged images are not certified correct.', 'SAM boundary suggestions cannot certify defect class.'])
    (output / 'summary.json').write_text(json.dumps(metadata, indent=2))
    model = None
    records = []
    for index, row in enumerate(tqdm(rows, desc='Training mask audit'), 1):
        p = dataset / 'train/images' / row['file']
        if not p.is_file():
            raise FileNotFoundError(p)
        with Image.open(p) as image:
            width, height = image.size
            rgb = image.convert('RGB')
            image_hash = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
        if image_hash != row['pixel_hash']:
            raise ValueError(f'Image differs from manifest: {p.name}')
        lp = dataset / 'train/labels' / Path(p.name).with_suffix('.txt')
        annotations, flags = read_annotations(lp, width, height)
        structural, masks, body = inspect_geometry(annotations, width, height, row['fruit'])
        flags.extend(structural)
        proposals = []
        if checkpoint:
            cache_path = cache / (hashlib.sha256((p.name + image_hash).encode()).hexdigest() + '.json')
            if cache_path.exists():
                predictions = json.loads(cache_path.read_text())
            else:
                if model is None:
                    from ultralytics import YOLO
                    model = YOLO(str(checkpoint))
                    if [model.names[i] for i in range(len(model.names))] != NAMES:
                        raise ValueError('Checkpoint class order differs from this dataset')
                result = model.predict(str(p), imgsz=imgsz, device=device, conf=0.2, retina_masks=True, verbose=False, save=False, max_det=100)[0]
                predictions = prediction_rows(result)
                tmp = cache_path.with_suffix('.tmp')
                tmp.write_text(json.dumps(predictions))
                tmp.replace(cache_path)
            disagreement, proposals = compare_predictions(annotations, masks, body, predictions, width, height)
            flags.extend(disagreement)
        record = dict(file=p.name, fruit=row['fruit'], split='train', width=width, height=height, label_sha256=source_snapshot[p.name], annotations=annotations, flags=flags, proposals=proposals, score=sum(f['weight'] for f in flags))
        records.append(record)
        if index % 25 == 0 or index == len(rows):
            print(f'Checked {index}/{len(rows)} training images | flagged {sum(bool(r["flags"]) for r in records)} | proposals {sum(len(r["proposals"]) for r in records)}', flush=True)
            (output / 'progress.json').write_text(json.dumps(dict(checked=index, total=len(rows), status='running')))
    if sam2_path:
        if not Path(sam2_path).is_file():
            raise FileNotFoundError(sam2_path)
        from ultralytics import SAM
        sam = None
        metadata['sam2_sha256'] = digest(sam2_path)
        metadata['sam_strategy'] = 'single-box-v2'
        sam_cache = output / 'sam_cache'
        sam_cache.mkdir(exist_ok=True)
        ranked = sorted([r for r in records if r['flags']], key=lambda r:-r['score'])[:sam_limit]
        for record in tqdm(ranked, desc='SAM boundary proposals'):
            targets = [a for a in record['annotations'] if a['class_id'] >= 2][:4]
            if not targets:
                continue
            boxes = []
            for a in targets:
                pts = np.asarray(a['points']) * [record['width'], record['height']]
                boxes.append([float(pts[:,0].min()), float(pts[:,1].min()), float(pts[:,0].max()), float(pts[:,1].max())])
            # Only the SAM cache changes. Completed YOLO predictions remain reusable.
            skey = hashlib.sha256(json.dumps(['single-box-v2', namespace, record['file'], record['label_sha256'], metadata['sam2_sha256'], boxes]).encode()).hexdigest()
            spath = sam_cache / (skey + '.json')
            if spath.exists():
                cached = json.loads(spath.read_text())
            else:
                cached = dict(strategy='single-box-v2', proposals=[])
            proposals = cached.get('proposals', [])
            for index in range(len(proposals), len(targets)):
                if sam is None:
                    sam = SAM(str(sam2_path))
                proposals.append(sam_proposal_for_box(
                    sam, dataset / 'train/images' / record['file'], boxes[index],
                    targets[index], record['width'], record['height'], device))
                # Save every prompt, including empty results, to resume after interruption.
                cached['proposals'] = proposals
                temp = spath.with_suffix('.tmp')
                temp.write_text(json.dumps(cached))
                temp.replace(spath)
            for a, proposal in zip(targets, proposals):
                pts = proposal['points']
                if pts is None:
                    record['flags'].append(dict(reason='sam_no_proposal', line=a['line'], detail=proposal.get('reason', 'no_mask'), weight=1))
                    continue
                points = np.clip(pts, 0, 1).tolist()
                old = raster(a['points'], record['width'], record['height'])
                new = raster(points, record['width'], record['height'])
                overlap = iou(old, new)
                if overlap < 0.5:
                    record['flags'].append(dict(reason='sam_boundary_disagreement', line=a['line'], iou=round(overlap, 4), weight=2))
                    record['proposals'].append(dict(class_id=a['class_id'], points=points, reason='sam_boundary_disagreement', approved=False, source='sam2_box_prompt', associated_line=a['line'], class_verified=False, candidate_count=proposal['candidate_count']))
            record['score'] = sum(f['weight'] for f in record['flags'])
    for r in rows:
        lp = dataset / 'train/labels' / Path(r['file']).with_suffix('.txt')
        assert source_snapshot[r['file']] == (digest(lp) if lp.is_file() else None), 'A source label changed during audit'
    result = write_reports(dataset, output, records, metadata, review_limit)
    (output / 'progress.json').write_text(json.dumps(dict(checked=len(rows), total=len(rows), status='complete')))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--imgsz', type=int, default=1024)
    parser.add_argument('--max-images', type=int, default=0)
    parser.add_argument('--sam2-path', type=Path)
    parser.add_argument('--sam-limit', type=int, default=40)
    args = parser.parse_args()
    run_audit(**vars(args))
