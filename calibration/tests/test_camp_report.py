"""标准跑姿报告消费契约；仅使用本地合成图片，不进行人体测量或部署。

运行：python3 -m unittest discover -s calibration/tests -p test_camp_report.py -v
"""
from __future__ import annotations
import tempfile

import copy
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import fitz
from docx import Document
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from camp_report import ITEMS, build_camp_data, export_camp_report
from measurement_report import build_payload


HEADERS = ('环节', '分项', '评判结果', '改进建议')
SCRATCH = Path(tempfile.gettempdir())


def compact(text):
    """PDF中文逐字排版产生的空白不是内容截断。"""
    return re.sub(r'\s+', '', text)


def docx_text(document):
    return '\n'.join(
        [p.text for p in document.paragraphs]
        + [c.text for t in document.tables for r in t.rows for c in r.cells]
    )


class CampReportTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.folder = TemporaryDirectory(prefix='camp-report-test-', dir=str(SCRATCH))
        self.addCleanup(self.folder.cleanup)
        self.work = Path(self.folder.name)
        self.payload = build_payload([], expected_frames=0)
        self.user = {'name': '合成夹具跑者', 'coach': '合成夹具教练'}

    def data(self):
        return build_camp_data(self.payload, self.user)

    def image(self, name='fixture.png', color='navy'):
        path = self.work / name
        # 单色图片仅检验嵌图，不暗示任何真实跑姿或拍摄方向。
        Image.new('RGB', (480, 270), color).save(path)
        return str(path)

    def export(self, data=None, keyframes=None, fmt='pdf', name='report'):
        path = self.work / (name + '.' + fmt)
        export_camp_report(self.data() if data is None else data,
                           [] if keyframes is None else keyframes,
                           str(path), fmt=fmt)
        self.assertTrue(path.is_file(), '导出接口未创建指定文件')
        self.assertGreater(path.stat().st_size, 0)
        return path

    def pdf_pages(self, path):
        with fitz.open(path) as document:
            return [page.get_text() for page in document]

    def test_items_and_data_schema_have_thirteen_rows(self):
        self.assertEqual(len(ITEMS), 13)
        self.assertEqual(len(set(ITEMS)), 13)
        for pair in ITEMS:
            self.assertIsInstance(pair, tuple)
            self.assertEqual(len(pair), 2)
            self.assertTrue(all(isinstance(v, str) and v for v in pair))
        self.assertIn('身体姿态', ''.join(v for pair in ITEMS for v in pair))
        data = self.data()
        self.assertTrue({'rows', 'name', 'coach', 'score', 'date', 'title',
                         'summary', 'notice'}.issubset(data))
        self.assertEqual(len(data['rows']), 13)
        for expected, row in zip(ITEMS, data['rows']):
            self.assertIsInstance(row, tuple)
            self.assertEqual(len(row), 4)
            self.assertEqual(row[:2], expected)
        self.assertEqual(data['name'], self.user['name'])
        self.assertEqual(data['coach'], self.user['coach'])

    def test_default_user_and_unknown_score_do_not_become_zero(self):
        for supplied in ({}, {'score': None}, {'score': '未知'}):
            with self.subTest(user_info=supplied):
                data = build_camp_data(self.payload, supplied)
                self.assertNotEqual(data['score'], 0)
                self.assertNotEqual(data['score'], '0')
                self.assertTrue(data['score'] is None or
                                re.search(r'未知|未评分|未验证|不评分', str(data['score'])))
        self.assertNotEqual(build_camp_data(self.payload)['score'], 0)
        self.assertIsNone(self.payload['score'])

    def test_build_and_export_do_not_mutate_inputs(self):
        original_payload = copy.deepcopy(self.payload)
        original_user = copy.deepcopy(self.user)
        data = self.data()
        original_data = copy.deepcopy(data)
        keyframes = [(self.image(), '合成图片：方向未知')]
        original_keyframes = copy.deepcopy(keyframes)
        for fmt in ('pdf', 'docx'):
            self.export(data, keyframes, fmt=fmt)
        self.assertEqual(self.payload, original_payload)
        self.assertEqual(self.user, original_user)
        self.assertEqual(data, original_data)
        self.assertEqual(keyframes, original_keyframes)

    def test_all_missing_rows_remain_unknown(self):
        for category, item, result, suggestion in self.data()['rows']:
            with self.subTest(category=category, item=item):
                self.assertRegex(str(result), r'未知|未验证|无法|不足|未评判')
                for false_claim in ('基本正常', '优秀', '前掌触地', '后跟触地'):
                    self.assertNotIn(false_claim, str(result))

    def test_pdf_two_pages_first_page_has_all_headers_and_items(self):
        path = self.export()
        pages = self.pdf_pages(path)
        self.assertEqual(len(pages), 2)
        first = compact(pages[0])
        for header in HEADERS:
            self.assertIn(header, first)
        for category, item in ITEMS:
            self.assertIn(compact(category), first)
            self.assertIn(compact(item), first)
        self.assertIn('身体姿态', first)
        self.assertRegex(first, r'未知|未验证|未评分')
        # 实际栅格化一页，覆盖字体/页面对象可被渲染的基本契约。
        with fitz.open(path) as document:
            pixmap = document[0].get_pixmap(matrix=fitz.Matrix(.5, .5))
            self.assertGreater(pixmap.width, 0)
            self.assertGreater(pixmap.height, 0)

    def test_pdf_missing_keyframes_does_not_invent_front_or_back(self):
        pages = self.pdf_pages(self.export())
        self.assertEqual(len(pages), 2)
        second = compact(pages[1])
        # 缺图说明可以提及方向，但不能伪造该方向的图片/观察。
        self.assertNotRegex(second, r'(正面|背面|后视|前视)(截图|关键帧|视角图|跑姿图)')
        self.assertRegex(second, r'暂无|缺少|未提供|无可用|没有|未知')
        with fitz.open(self.work / 'report.pdf') as document:
            # The sample's brand header is an image, not a fabricated runner.
            images = document[1].get_image_info()
            self.assertFalse([image for image in images if image['bbox'][1] > 85])

    def test_pdf_uses_supplied_image_and_literal_caption(self):
        caption = '合成单色夹具：方向未知，不是正面或背面证据'
        path = self.export(keyframes=[(self.image(), caption)])
        pages = self.pdf_pages(path)
        self.assertEqual(len(pages), 2)
        self.assertIn(compact(caption), compact(pages[1]))
        with fitz.open(path) as document:
            self.assertTrue(document[1].get_images(full=True))

    def test_docx_four_column_table_items_images_and_captions(self):
        captions = ['合成红色图：拍摄方向未知', '合成蓝色图：没有触地证据']
        keyframes = [(self.image('red.png', 'red'), captions[0]),
                     (self.image('blue.png', 'blue'), captions[1])]
        document = Document(self.export(keyframes=keyframes, fmt='docx'))
        tables = [table for table in document.tables
                  if len(table.columns) == 4 and
                  tuple(cell.text for cell in table.rows[0].cells) == HEADERS]
        self.assertEqual(len(tables), 1, '应有一个带四列标准表头的评判表')
        table = tables[0]
        self.assertEqual(len(table.rows), 14)
        for expected, row in zip(ITEMS, table.rows[1:]):
            self.assertEqual(row.cells[1].text, expected[1])
        text = docx_text(document)
        for caption in captions:
            self.assertIn(caption, text)
        self.assertEqual(len(document.inline_shapes), 2)
        self.assertIn('身体姿态', text)

    def test_xml_like_name_is_preserved_literally_in_pdf_and_docx(self):
        name = '跑者&<b>非标签</b><未知>测试'
        data = build_camp_data(self.payload, {'name': name})
        self.assertEqual(data['name'], name)
        pdf = self.export(data, fmt='pdf')
        self.assertIn(compact(name), compact('\n'.join(self.pdf_pages(pdf))))
        document = Document(self.export(data, fmt='docx'))
        self.assertIn(name, docx_text(document))

    def test_long_text_is_not_truncated_in_either_format(self):
        data = self.data()
        # 超过通常一行/单格长度；首尾及中段一起核验，禁止省略号截断。
        long_summary = ('长段摘要保留全部文字，当前样本没有可验证的跑姿结论。' * 35
                        + '摘要唯一结束标记XYZ')
        long_suggestion = ('缺少可靠证据，不应虚构技术诊断或训练建议。' * 30
                           + '建议唯一结束标记ABC')
        data['summary'] = long_summary
        cat, item, result, _ = data['rows'][0]
        data['rows'] = [(cat, item, result, long_suggestion)] + list(data['rows'][1:])
        pdf = self.export(data, fmt='pdf', name='long')
        pdf_text = compact('\n'.join(self.pdf_pages(pdf)))
        document = Document(self.export(data, fmt='docx', name='long'))
        word_text = compact(docx_text(document))
        for expected in (long_summary, long_suggestion):
            self.assertIn(compact(expected), pdf_text)
            self.assertIn(compact(expected), word_text)

    def test_seven_photos_two_pages_and_overflow_is_not_lost(self):
        for count in (7, 9):
            with self.subTest(count=count):
                frames = [(self.image(f'photo-{i}.png'), f'夹具截图编号{i}')
                          for i in range(count)]
                path = self.export(keyframes=frames, name=f'photos-{count}')
                pages = self.pdf_pages(path)
                if count == 7:
                    self.assertEqual(len(pages), 2)
                for _, caption in frames:
                    self.assertIn(caption, compact(''.join(pages)))

    def test_sidecar_diagnoses_are_not_reused_in_captions(self):
        import json
        from camp_report import collect_keyframes
        path = self.image('keyframe_01.jpg')
        Path(path).with_name('keyframe_01_info.json').write_text(json.dumps({
            'frame_index': 4, 'timestamp': 0.4,
            'metrics': {'running_phase': 'heel_strike', 'score': 99}}))
        frames = collect_keyframes(self.work)
        self.assertEqual(len(frames), 1)
        self.assertIn('帧索引 4', frames[0][1])
        self.assertIn('0.400秒', frames[0][1])
        self.assertNotIn('heel_strike', frames[0][1])
        self.assertNotIn('99', frames[0][1])

    def test_normal_unscored_generate_report_uses_standard_template(self):
        from report_generator import ReportGenerator
        user = {'name': '正常无评分入口'}
        original_user = copy.deepcopy(user)
        results = []
        for fmt in ('pdf', 'docx'):
            with self.subTest(fmt=fmt):
                path = self.work / ('normal.' + fmt)
                ReportGenerator().generate_report('synthetic-no-video.mp4', user,
                                                  results, str(path), fmt=fmt)
                self.assertTrue(path.is_file())
                if fmt == 'pdf':
                    pages = self.pdf_pages(path)
                    self.assertEqual(len(pages), 2)
                    text = compact(pages[0])
                else:
                    document = Document(path)
                    text = compact(docx_text(document))
                for header in HEADERS:
                    self.assertIn(header, text)
                for _, item in ITEMS:
                    self.assertIn(compact(item), text)
        self.assertEqual(user, original_user)
        self.assertEqual(results, [])


if __name__ == '__main__':
    unittest.main()
