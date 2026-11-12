'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../../static/trend.html'), 'utf8');
const start = html.indexOf('async function loadProgress()');
const source = html.slice(start, html.indexOf('</script>', start));
let current;
const elements = {progressCard: {style: {}}, progressBody: {innerHTML: ''}};
const context = vm.createContext({
    document: {getElementById: id => elements[id]},
    escapeHtml: x => String(x),
    apiFetch: async () => ({ok: true, json: async () => ({progress: {
        score_change: current, total_analyses: 2, improvements: [], regressions: []}})}),
});
vm.runInContext(source, context);
(async () => {
    let count = 0;
    for (const value of [null, NaN, Infinity, -Infinity, '0', undefined]) {
        current = value;
        await context.loadProgress();
        assert.match(elements.progressBody.innerHTML, /未评分／不可比较/);
        assert.doesNotMatch(elements.progressBody.innerHTML, /#080|#c00/);
        count++;
    }
    current = 0;
    await context.loadProgress();
    assert.match(elements.progressBody.innerHTML, /\+0/);
    assert.match(elements.progressBody.innerHTML, /#080/);
    console.log(`${++count} tests passed (actual trend.html progress function; Node DOM stub).`);
})().catch(error => {console.error(error); process.exitCode = 1;});
