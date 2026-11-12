'use strict';
// No dependencies: execute the actual inline results page script in Node's VM.
// Run: node calibration/tests/test_results_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../static/results.html'), 'utf8');

class Element {
    constructor() { this.style = {}; this.children = []; this._html = ''; this._text = ''; this.onclick = null; }
    set innerHTML(value) { this._html = String(value); this._text = ''; this.children = []; }
    get innerHTML() { return this._html + this.children.map(c => c.innerHTML || c.textContent).join(''); }
    set textContent(value) { this._text = String(value); this._html = ''; this.children = []; }
    get textContent() { return this._text + this._html + this.children.map(c => c.textContent).join(''); }
    appendChild(child) { this.children.push(child); return child; }
}
const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element()]));
const get = id => {
    assert.ok(elements.has(id), 'Actual HTML must declare #' + id);
    return elements.get(id);
};
const escapeHtml = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const context = vm.createContext({
    document: {getElementById: get, createElement: () => new Element()},
    window: {}, localStorage: {getItem: () => null}, escapeHtml,
    apiFetch: () => { throw new Error('Unexpected network request'); },
    console, URL, setTimeout,
});
for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
    if (!/\bsrc=/.test(match[1])) vm.runInContext(match[2], context, {filename: 'results.html'});
}
let tests = 0;
function test(name, run) { run(); tests++; console.log('PASS ' + name); }
function show(data) { context.displayResults(data); }
function metrics(value) { return {avg_knee_flexion: value, avg_shin_angle: value, body_leang: value}; }
function gait(value) {
    return {cadence: value, vertical_oscillation: value, stride_length_m: value,
        ground_contact_time_ms: value, stance_ratio: value, step_symmetry: value,
        valid_frame_ratio: value, strike_posture: {left_forward_deg: value, right_forward_deg: value, over_stride_ratio: value}};
}
function breakdown(score) { return [{category: '环节', item: '分项', result: '结果', score}]; }
const legacy = {
    score: 90, grade: '优秀', metrics: {...metrics(10), stride_level: '旧跨步结论', symmetry_level: '旧对称结论'},
    gait: {...gait(1), cadence_level: '旧步频结论', strike_posture: {over_stride_ratio: 0.5, over_stride_level: '旧风险结论'}},
    training_plan: {focus_areas: ['旧训练计划'], weekly_plan: [['周一', '训练', '目标']]},
    problems: ['旧问题'], suggestions: ['旧建议'], score_breakdown: breakdown(90),
    video_quality: {duration: 5, fps: 30, valid_frame_ratio: 1}, report_file: 'old.json',
};

test('finite zero score, all three angles and every gait numeric field are retained', () => {
    show({score: 0, grade: '低分', metrics: metrics(0), gait: gait(0), score_breakdown: breakdown(0)});
    assert.equal(get('scoreNum').textContent, '0');
    assert.equal(get('scoreGrade').textContent, '低分');
    for (const id of ['kneeAngle', 'shinAngle', 'bodyLeang']) assert.equal(get(id).textContent, '0.0°');
    const grid = get('gaitGrid').innerHTML;
    for (const expected of ['0.0 步/分', '0.0%（腿长）', '0.00 m', '0 ms', '0%', '0.0° / 0.0°']) assert.ok(grid.includes(expected), expected);
    assert.match(get('breakdownBody').innerHTML, /color:#c00/);
    assert.match(get('breakdownBody').innerHTML, />0<\/td>/);
});
for (const value of [null, NaN, Infinity, -Infinity, '12.3', '', false, undefined]) {
    test('invalid numeric value rejected: ' + String(value) + ' (' + typeof value + ')', () => {
        show({score: value, grade: '不应评级', metrics: metrics(value), gait: gait(value), score_breakdown: breakdown(value)});
        assert.equal(get('scoreNum').textContent, '未评分');
        assert.equal(get('scoreGrade').textContent, '');
        assert.equal(get('scoreUnit').style.display, 'none');
        assert.equal(get('scoreBanner').style.background, '#6c757d');
        for (const id of ['kneeAngle', 'shinAngle', 'bodyLeang']) assert.equal(get(id).textContent, '-');
        // Test gait formatting independently in scored mode, not only its unscored gate.
        show({score: 80, metrics: metrics(value), gait: gait(value), score_breakdown: breakdown(value)});
        assert.doesNotMatch(get('gaitGrid').innerHTML, /NaN|Infinity|12\.3|false|undefined|null/);
        assert.match(get('gaitGrid').innerHTML, /步频:<\/span><span class="metric-value">-</);
        assert.match(get('breakdownBody').innerHTML, /color:#6c757d/);
        assert.match(get('breakdownBody').innerHTML, /未评分/);
        assert.doesNotMatch(get('breakdownBody').innerHTML, /color:#080|color:#c00/);
    });
}
for (const status of ['unscored_2d_geometry_only', 'unscored_future_status', 'insufficient_evidence']) {
    test('status overrides stale finite score and technical/training content: ' + status, () => {
        show({...legacy, measurement_status: status, measurement_limitations: ['足部遮挡', '<script>bad</script>']});
        assert.equal(get('scoreNum').textContent, '未评分');
        assert.equal(get('scoreGrade').textContent, '');
        assert.equal(get('gaitGrid').innerHTML, '');
        assert.equal(get('trainingPlanCard').style.display, 'none');
        assert.equal(get('trainingPlanBody').innerHTML, '');
        assert.equal(get('adviceCard').style.display, 'none');
        assert.equal(get('problemsList').innerHTML, '');
        assert.equal(get('suggestionsList').innerHTML, '');
        assert.equal(get('strideLevel').textContent, '-');
        assert.equal(get('symmetryLevel').textContent, '-');
        assert.match(get('measurementNotice').innerHTML, /足部遮挡/);
        assert.match(get('measurementNotice').innerHTML, /&lt;script&gt;/);
        assert.doesNotMatch(get('measurementNotice').innerHTML, /<script>/);
        assert.match(get('breakdownBody').innerHTML, /color:#6c757d/);
    });
}
test('fallback explains insufficient evidence without server limitations', () => {
    show({score: null});
    assert.match(get('measurementNotice').innerHTML, /缺少可验证的评分证据/);
});
test('coverage labels denominator/full timeline/non-accuracy and preserves zero', () => {
    show({score: null, measurement_coverage: {
        knee_flexion: {value: 0, valid_frames: 0, expected_frames: 20, coverage: 0},
        shin_angle: {valid_frames: 5, expected_frames: 20, coverage: 0.25},
        body_lean: {valid_frames: 3, expected_frames: null, coverage: null},
        zero_denominator: {valid_frames: 0, expected_frames: 0, coverage: 1},
        '<unsafe>': {valid_frames: 2, expected_frames: 20, coverage: '0.1'},
    }});
    const coverage = get('measurementCoverage').textContent;
    assert.match(coverage, /不是准确率/);
    assert.match(coverage, /包括检测失败帧/);
    assert.match(coverage, /有效帧 0 \/ 预期帧（分母）20；覆盖率 0\.0%/);
    assert.match(coverage, /有效帧 5 \/ 预期帧（分母）20；覆盖率 25\.0%/);
    assert.match(coverage, /有效帧 3 \/ 预期帧（分母）未知或为0；覆盖率 未知/);
    assert.match(coverage, /zero_denominator：有效帧 0 \/ 预期帧（分母）未知或为0；覆盖率 未知/);
    assert.equal(get('measurementCoverage').children[4].textContent, '<unsafe>：有效帧 2 / 预期帧（分母）20；覆盖率 未知');
    assert.equal(get('measurementCoverage').children[4]._html, '');
});
test('empty and partial quality never claim good quality; nonfinite quality is safe', () => {
    context.renderQuality({});
    assert.equal(get('qualityWrap').style.display, 'none');
    assert.equal(get('qualityBody').innerHTML, '');
    context.renderQuality({duration: '12', fps: Infinity, valid_frame_ratio: NaN});
    assert.doesNotMatch(get('qualityBody').innerHTML, /✓ 视频质量良好|NaN|Infinity|12s/);
    context.renderQuality({warnings: ['重拍'], reasons: ['遮挡']});
    assert.match(get('qualityBody').innerHTML, /重拍/);
    assert.match(get('qualityBody').innerHTML, /遮挡/);
});
test('two complete scored→unscored→scored switches clear and restore UI', () => {
    for (let i = 0; i < 2; i++) {
        show(legacy);
        assert.equal(get('trainingPlanCard').style.display, 'block');
        assert.match(get('trainingPlanBody').innerHTML, /旧训练计划/);
        assert.match(get('gaitGrid').innerHTML, /旧步频结论/);
        assert.equal(get('downloadRow').style.display, 'block');
        assert.equal(typeof get('downloadReportBtn').onclick, 'function');
        show({score: null, metrics: metrics(0), video_quality: {}, measurement_limitations: ['本次证据不足'],
            measurement_coverage: {knee_flexion: {valid_frames: 1, expected_frames: 2, coverage: 0.5}}});
        for (const id of ['gaitGrid', 'trainingPlanBody', 'problemsList', 'suggestionsList', 'breakdownBody', 'qualityBody']) assert.equal(get(id).innerHTML, '', id);
        for (const id of ['trainingPlanCard', 'adviceCard', 'breakdownWrap', 'qualityWrap', 'downloadRow']) assert.equal(get(id).style.display, 'none', id);
        assert.equal(get('downloadReportBtn').onclick, null);
        show(legacy);
        assert.equal(get('scoreBanner').style.background, '');
        assert.equal(get('scoreBanner').style.boxShadow, '');
        assert.equal(get('scoreUnit').style.display, '');
        assert.equal(get('scoreGrade').textContent, '优秀');
        assert.equal(get('measurementNotice').style.display, 'none');
        assert.equal(get('measurementNotice').innerHTML, '');
        assert.equal(get('measurementCoverage').style.display, 'none');
        assert.equal(get('measurementCoverage').innerHTML, '');
        assert.equal(get('adviceCard').style.display, 'block');
        assert.equal(get('breakdownWrap').style.display, 'block');
    }
});
test('unscored clears legacy screenshots without requesting them', () => {
    get('keyframesGallery').innerHTML = '旧技术标签截图';
    get('keyframesWrap').style.display = 'block';
    show({score: null, measurement_status: 'insufficient_evidence', keyframes_dir: 'old', keyframes_extracted: 5});
    assert.equal(get('keyframesGallery').innerHTML, '');
    assert.equal(get('keyframesWrap').style.display, 'none');
});
test('automatic primary subject shows frame zero/finite box without claiming identity or scoring', () => {
    show({score: null, measurement_status: 'unscored_2d_geometry_only', video_timeline: {subject_selection: {
        mode: 'automatic_primary_runner', initial_frame_index: 0, initial_box: [0.1,0.2,0.5,0.9]}}});
    const text = get('measurementNotice').innerHTML;
    assert.match(text, /首次选择帧索引 0/);
    assert.match(text, /10.0% \/ 20.0% \/ 50.0% \/ 90.0%/);
    assert.match(text, /非身份认证/); assert.match(text, /不自动换人/);
    assert.equal(get('scoreNum').textContent, '未评分');
});
test('malformed automatic subject evidence stays unknown and is cleared on next result', () => {
    show({score: null, video_timeline: {subject_selection: {mode: 'automatic_primary_runner',
        initial_frame_index: '<img>', initial_box: [0, 0, '1', Infinity]}}});
    assert.match(get('measurementNotice').innerHTML, /帧索引 未知/);
    assert.match(get('measurementNotice').innerHTML, /相对框 未知/);
    assert.doesNotMatch(get('measurementNotice').innerHTML, /<img>|Infinity/);
    show({score: null}); assert.doesNotMatch(get('measurementNotice').innerHTML, /自动主体选择记录/);
});
test('unknown and joint-masked frame counts are explicit, finite, and not accuracy', () => {
    for (const [outliers, masked, unknownCount] of [[[95,101],11,2],[[],0,0]]) {
        show({score:null, video_timeline:{decoded_frames:187,subject_selection:{mode:'automatic_primary_runner',
            initial_frame_index:0,initial_box:[.1,.1,.5,.9],pose_outlier_frame_indices:outliers,measurement_masked_frames:masked}}});
        const text=get('measurementNotice').innerHTML;
        assert.match(text,new RegExp('检测异常保留未知：'+unknownCount+' 帧'));
        assert.match(text,new RegExp('被屏蔽关节的帧：'+masked+' 帧'));
        assert.match(text,/未知帧和歧义关节未补算/);
        assert.match(text,/不代表测量准确率/);
        assert.equal(get('scoreNum').textContent,'未评分');
    }
});
test('invalid and duplicated outlier evidence is unknown, never fabricated zero', () => {
    for (const outliers of [[1,'2'],[1,1],[187],[-1]]) {
        show({score:null,video_timeline:{decoded_frames:187,subject_selection:{mode:'automatic_primary_runner',
            pose_outlier_frame_indices:outliers,measurement_masked_frames:'11'}}});
        assert.match(get('measurementNotice').innerHTML,/检测异常保留未知：未知 帧/);
        assert.match(get('measurementNotice').innerHTML,/被屏蔽关节的帧：未知 帧/);
    }
    show({score:null});assert.doesNotMatch(get('measurementNotice').innerHTML,/被屏蔽关节的帧/);
});
console.log(`\n${tests} tests passed (actual results.html script; Node DOM stub; no dependencies).`);
