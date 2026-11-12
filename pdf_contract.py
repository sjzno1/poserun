"""Separate PDF tool/page/text/render gate, only in test image."""
import json
from pathlib import Path
import fitz
outputs=json.loads(Path('/scratch/report-outputs.json').read_text())
with fitz.open(outputs['pdf']) as pdf:
    assert len(pdf)==2,len(pdf)
    text=''.join(p.get_text() for p in pdf)
    assert '未知' in text and '基本正常' not in text
    for i in range(2):pdf[i].get_pixmap().save('/scratch/report-page%d.png'%(i+1))
print('PDF_PAGES_TEXT_RENDER_PASS=2')
