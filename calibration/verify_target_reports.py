"""Inspect real API PDFs and re-export saved real measurements to DOCX locally."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import fitz
    from docx import Document
    from measurement_safety import safe_metrics
    from measurement_report import export_reports
    output = ROOT/'calibration/results-target-stage6'
    checks = []
    for name in ('kipchoge', 'kiptum'):
        frames = json.loads((output/(name+'-frames.json')).read_text())
        payload = safe_metrics(frames)['measurement_payload']
        paths = export_reports(payload, output/(name+'-report-inspection'))
        doc = Document(paths['docx'])
        word = ''.join(p.text for p in doc.paragraphs)
        with fitz.open(output/(name+'-actual-video.pdf')) as pdf:
            text = ''.join(page.get_text() for page in pdf)
            compact = ''.join(text.split())
            notices = ['身份未经独立验证', '总分：未知／未验证']
            tracking = payload['video_timeline']['target_tracking']
            if tracking['state'] == 'lost':
                notices.extend(['不自动换人', tracking['lost_reason'],
                                '帧索引'+str(tracking['first_lost_frame'])+'（从0计）'])
            for notice in notices:
                assert notice in word, (name, 'DOCX', notice)
                assert notice in compact, (name, 'real API PDF', notice)
            assert 'NaN' not in text and 'Infinity' not in text
            pdf[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(output/(name+'-actual-video-page1.png'))
            checks.append({'sample': name, 'api_pdf_pages': len(pdf),
                           'docx_paragraphs': len(doc.paragraphs),
                           'loss_and_unknown_notices_present': True,
                           'rendered_first_page': True})
    (output/'report-inspection.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
